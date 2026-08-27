#!/usr/bin/env python3
"""從分點 tape 累積淨買重建「估計持倉變化」與加權成本（研究用 · 非個人持倉）.

⚠️ 三個必讀限制：
  1. **分點 ≠ 個人**。`H3-BRANCH-EQUALS-INDIVIDUAL` 已 rejected——9217 觸及 2,243 檔、
     9661 觸及 2,348 檔，遠超單一大戶可能持續追蹤的標的數，這是**全客戶流量聚合**
     （含其他散戶、法人、造市），不是某一個人的部位。
  2. **這是變化量不是存量**。我們不知道窗口起點的庫存，只能算「自 --since 起淨增減多少」。
     負值代表期間淨賣出，不代表「做空」。
  3. **加權成本只在淨買方向為正時有意義**，且用日收盤近似（無盤中價位級資料）。
     取回 price-level 資料可精確化，見 docs/songshan-copytrade-research-round11.md M-06。

  PYTHONPATH=src .venv/bin/python scripts/research/branch_position_reconstruct.py --branch 9217
"""
from __future__ import annotations
import argparse, sqlite3, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT / "src"))
from stock_db import DEFAULT_DB_PATH  # noqa: E402

SOURCE = "finmind"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path(DEFAULT_DB_PATH))
    ap.add_argument("--branch", required=True)
    ap.add_argument("--since", default="")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--week-days", type=int, default=5)
    ap.add_argument("--min-mkt-yi", type=float, default=0.0,
                    help="只列估市值 >= N 億的部位（預設 0 = 全列）")
    ap.add_argument("--min-day-yi", type=float, default=0.0,
                    help="只累積『|單日淨額| >= N 億』的交易日——大買大賣都計入才會互相抵銷，"
                         "濾掉散戶零星流量；預設 0 = 全累積")
    ap.add_argument("--roll-days", type=int, default=0,
                    help="連續 N 日模式：滾動 N 日淨額合計絕對值 >= --roll-yi 才累積那幾天"
                         "（篩出持續建倉，而非單日爆量）")
    ap.add_argument("--roll-yi", type=float, default=1.5,
                    help="--roll-days 模式的滾動合計門檻（億）")
    a = ap.parse_args()
    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True); c.row_factory = sqlite3.Row

    lo, hi, ndays = c.execute(
        "SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date) "
        "FROM stock_broker_branch_daily WHERE securities_trader_id=? AND source=?",
        (a.branch, SOURCE)).fetchone()
    since = a.since or lo
    days = [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM stock_broker_branch_daily WHERE securities_trader_id=? "
        "AND source=? ORDER BY trade_date DESC LIMIT ?", (a.branch, SOURCE, a.week_days))]
    wk_start = days[-1] if days else hi
    name = c.execute("SELECT securities_trader FROM stock_broker_branch_daily WHERE securities_trader_id=? LIMIT 1",
                     (a.branch,)).fetchone()
    print(f"分點 {a.branch} {name[0] if name else ''} · tape {lo} ~ {hi}（{ndays} 個交易日）")
    print(f"累積區間：{since} ~ {hi}　|　近一週：{wk_start} ~ {hi}（{len(days)} 個交易日）\n")

    dthr = a.min_day_yi * 1e8
    rows = c.execute(
        """
        SELECT b.stock_id,
               SUM(CASE WHEN ABS((b.buy-b.sell)*p.close) >= ? THEN b.buy - b.sell ELSE 0 END) AS net_sh,
               SUM(CASE WHEN ABS((b.buy-b.sell)*p.close) >= ? THEN b.buy * p.close ELSE 0 END) AS buy_ntd,
               SUM(CASE WHEN ABS((b.buy-b.sell)*p.close) >= ? THEN b.sell * p.close ELSE 0 END) AS sell_ntd,
               SUM(CASE WHEN b.buy>b.sell AND ABS((b.buy-b.sell)*p.close) >= ? THEN (b.buy-b.sell)*p.close ELSE 0 END) AS acc_ntd,
               SUM(CASE WHEN b.buy>b.sell AND ABS((b.buy-b.sell)*p.close) >= ? THEN (b.buy-b.sell)      ELSE 0 END)    AS acc_sh,
               SUM(CASE WHEN b.trade_date>=? THEN b.buy-b.sell ELSE 0 END)        AS wk_net_sh,
               SUM(CASE WHEN b.trade_date>=? THEN (b.buy-b.sell)*p.close ELSE 0 END) AS wk_net_ntd
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
        WHERE b.securities_trader_id=? AND b.source=? AND b.trade_date>=?
          AND length(b.stock_id)=4 AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]' AND b.stock_id NOT GLOB '00*'
        GROUP BY b.stock_id
        """, (dthr, dthr, dthr, dthr, dthr, wk_start, wk_start, SOURCE, a.branch, SOURCE, since)).fetchall()

    last = {r[0]: float(r[1]) for r in c.execute(
        "SELECT b.stock_id, b.close FROM stock_daily_bars b JOIN "
        "(SELECT stock_id, MAX(trade_date) md FROM stock_daily_bars WHERE source=? GROUP BY stock_id) m "
        "ON m.stock_id=b.stock_id AND m.md=b.trade_date WHERE b.source=? AND b.close>0", (SOURCE, SOURCE))}

    roll_over: dict[str, dict] = {}
    if a.roll_days:
        # 逐股拉日序，算滾動 N 日淨額；命中的窗口內每一天都計入累積。
        daily = c.execute(
            f"""
            SELECT b.stock_id AS sid, b.trade_date AS d,
                   (b.buy-b.sell) AS net_sh, (b.buy-b.sell)*p.close AS net_ntd, p.close AS px
            FROM stock_broker_branch_daily b
            JOIN stock_daily_bars p ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
            WHERE b.securities_trader_id=? AND b.source=? AND b.trade_date>=?
              AND length(b.stock_id)=4 AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
              AND b.stock_id NOT GLOB '00*'
            ORDER BY b.stock_id, b.trade_date
            """, (SOURCE, a.branch, SOURCE, since)).fetchall()
        per: dict[str, list] = {}
        for r in daily:
            per.setdefault(r["sid"], []).append(
                (r["d"], float(r["net_sh"] or 0), float(r["net_ntd"] or 0), float(r["px"] or 0)))
        thr_ntd = a.roll_yi * 1e8
        for sid, seq in per.items():
            keep = [False] * len(seq)
            for i in range(a.roll_days - 1, len(seq)):
                w = seq[i - a.roll_days + 1: i + 1]
                if abs(sum(x[2] for x in w)) >= thr_ntd:
                    for k in range(i - a.roll_days + 1, i + 1):
                        keep[k] = True
            net_sh = sum(seq[i][1] for i in range(len(seq)) if keep[i])
            if abs(net_sh) < 1000:
                continue
            acc_sh = sum(seq[i][1] for i in range(len(seq)) if keep[i] and seq[i][1] > 0)
            acc_ntd = sum(seq[i][2] for i in range(len(seq)) if keep[i] and seq[i][1] > 0)
            wk_sh = sum(seq[i][1] for i in range(len(seq)) if keep[i] and seq[i][0] >= wk_start)
            wk_ntd = sum(seq[i][2] for i in range(len(seq)) if keep[i] and seq[i][0] >= wk_start)
            n_days = sum(keep)
            roll_over[sid] = dict(net_sh=net_sh, acc_sh=acc_sh, acc_ntd=acc_ntd,
                                  wk_sh=wk_sh, wk_ntd=wk_ntd, n_days=n_days)

    recs = []
    for r in rows:
        if a.roll_days:
            ro = roll_over.get(r["stock_id"])
            if not ro:
                continue
            net_sh = ro["net_sh"]
            cost = (ro["acc_ntd"] / ro["acc_sh"]) if ro["acc_sh"] > 0 else None
        else:
            net_sh = float(r["net_sh"] or 0)
            if abs(net_sh) < 1000:
                continue
            cost = (float(r["acc_ntd"]) / float(r["acc_sh"])) if float(r["acc_sh"] or 0) > 0 else None
        px = last.get(r["stock_id"])
        recs.append({
            "sid": r["stock_id"], "net_sh": net_sh,
            "net_ntd": float(r["buy_ntd"] or 0) - float(r["sell_ntd"] or 0),
            "cost": cost, "px": px,
            "mkt": (net_sh * px) if px else None,
            "pnl_pct": ((px / cost - 1) * 100) if (cost and px and net_sh > 0) else None,
            "wk_sh": (roll_over[r["stock_id"]]["wk_sh"] if a.roll_days else float(r["wk_net_sh"] or 0)),
            "wk_ntd": (roll_over[r["stock_id"]]["wk_ntd"] if a.roll_days else float(r["wk_net_ntd"] or 0)),
            "n_days": (roll_over[r["stock_id"]]["n_days"] if a.roll_days else None),
        })

    thr = a.min_mkt_yi * 1e8
    longs = sorted([x for x in recs if x["net_sh"] > 0 and (x["mkt"] or 0) >= thr],
                   key=lambda x: -(x["mkt"] or 0))
    parts=[]
    if a.min_mkt_yi: parts.append(f"估市值 >= {a.min_mkt_yi} 億")
    if a.min_day_yi: parts.append(f"只累積 |單日淨額| >= {a.min_day_yi} 億的交易日")
    if a.roll_days: parts.append(f"只累積『滾動 {a.roll_days} 日淨額合計 >= {a.roll_yi} 億』命中的日子")
    filt = f"（{' · '.join(parts)}）" if parts else ""
    print(f"=== 期間淨買部位{filt}·前 {a.top}（估計，非真實庫存）===")
    print(f"{'股號':<7}{'淨買(張)':>11}{'估市值(億)':>11}{'加權成本':>10}{'現價':>9}{'損益%':>9}{'近一週(張)':>11}{'命中日':>7}")
    tot = 0.0
    for x in longs[:a.top]:
        tot += x["mkt"] or 0
        pnl = f"{x['pnl_pct']:+.1f}" if x["pnl_pct"] is not None else "—"
        cs = f"{x['cost']:.1f}" if x["cost"] else "—"
        ps = f"{x['px']:.1f}" if x["px"] else "—"
        print(f"{x['sid']:<7}{x['net_sh']/1000:>11,.0f}{(x['mkt'] or 0)/1e8:>11.2f}{cs:>10}{ps:>9}{pnl:>9}{x['wk_sh']/1000:>11,.0f}{(x['n_days'] if x['n_days'] is not None else 0):>7}")
    allmkt = sum(x["mkt"] or 0 for x in longs)
    print(f"\n  列出 {min(a.top,len(longs))} 檔合計 {tot/1e8:.1f} 億 · 通過門檻者 {len(longs)} 檔／合計 {allmkt/1e8:.1f} 億")

    wk = sorted(recs, key=lambda x: -x["wk_ntd"])
    print(f"\n=== 近一週淨買 TOP 8 ===")
    for x in wk[:8]:
        print(f"  {x['sid']:<7}{x['wk_sh']/1000:>10,.0f} 張   {x['wk_ntd']/1e8:>+8.2f} 億")
    print(f"=== 近一週淨賣 TOP 8 ===")
    for x in wk[-8:][::-1]:
        print(f"  {x['sid']:<7}{x['wk_sh']/1000:>10,.0f} 張   {x['wk_ntd']/1e8:>+8.2f} 億")
    wt = sum(x["wk_ntd"] for x in recs)
    print(f"\n  近一週全部標的淨額合計：{wt/1e8:+.2f} 億")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
