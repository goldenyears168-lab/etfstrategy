"""鎖跌停版本的 limit_up_locker_walkforward_verify.py（鏡像對照組）。

方法論跟漲停版完全一樣、只是反過來：找「一價到底式鎖死跌停」（收盤=理論跌停價、
且全天最高點沒站上跌停 = 全天無人願意用比跌停高的價格承接），對每個事件找當天
淨賣超最大的分點（可能是恐慌出場的散戶，也可能是主力出貨/放空回補），PIT-safe
判定「重複出現的鎖手」（>=3次前科才算數，避免look-ahead），隔天有可交易股票期貨
才納入，用『隔天開盤進場、隔天收盤出場』這個唯一可執行的口徑算報酬。

假說開放兩種可能，讓資料自己說話，不預設方向：
  A) 恐慌出清後反彈（隔天做多有edge，鎖死跌停=賣壓已經耗盡）
  B) 恐慌延續（隔天做空有edge，鎖死跌停=真的有壞消息，隔天繼續破）
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

import stock_db
from order.dayflip_short_signal import resolve_futures_symbol

DB_PATH = stock_db.DEFAULT_DB_PATH
START_DATE = "2024-07-01"
MIN_SELL_NTD = 3_000_000
PRIOR_COUNT_MIN = 3


def tick_size(price: float) -> float:
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def limit_down_price(prev_close: float) -> float:
    theo = prev_close * 0.90
    ts = tick_size(theo)
    n = int(round(theo / ts - 1e-9))
    # 無條件進位到跳動點位（不能低於理論-10%下限）
    while n * ts < theo - 1e-6:
        n += 1
    return round(n * ts, 2)


def main() -> None:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    rows = con.execute(
        "SELECT stock_id, trade_date, open, high, low, close FROM stock_daily_bars "
        "WHERE source='finmind' AND trade_date >= ? AND close > 0 "
        "ORDER BY stock_id, trade_date",
        (START_DATE,),
    ).fetchall()

    by_stock: dict[str, list[tuple]] = defaultdict(list)
    for sid, d, o, h, l, c in rows:
        by_stock[str(sid)].append((d, o, h, l, c))

    strong_events = []
    for sid, series in by_stock.items():
        if sid.startswith("00"):
            continue
        for i in range(1, len(series)):
            d, o, h, l, c = series[i]
            prev_c = series[i - 1][4]
            if prev_c <= 0:
                continue
            lim = limit_down_price(prev_c)
            if abs(c - lim) > 0.011:
                continue
            if h <= lim + 1e-6:  # 全天沒站上跌停 = 一價到底式鎖死
                strong_events.append({
                    "stock_id": sid, "date": d, "close": c,
                    "next_date": series[i + 1][0] if i + 1 < len(series) else None,
                    "next_open": series[i + 1][1] if i + 1 < len(series) else None,
                    "next_close": series[i + 1][4] if i + 1 < len(series) else None,
                })

    strong_events.sort(key=lambda e: e["date"])
    n_stocks = len({e["stock_id"] for e in strong_events})
    print(f"強鎖跌停事件總數: {len(strong_events)}（{n_stocks}檔不同股票）")

    for e in strong_events:
        rows2 = con.execute(
            "SELECT securities_trader_id, buy, sell FROM stock_broker_branch_daily "
            "WHERE stock_id=? AND trade_date=?", (e["stock_id"], e["date"]),
        ).fetchall()
        px = e["close"]
        ranked = []
        for tid, b, s in rows2:
            net_sell_amt = (float(s or 0) - float(b or 0)) * px
            if net_sell_amt >= MIN_SELL_NTD:
                ranked.append((tid, net_sell_amt))
        ranked.sort(key=lambda x: -x[1])
        e["top_tids"] = [tid for tid, _ in ranked[:3]]

    tid_prior_count: dict[str, int] = defaultdict(int)
    actionable = []
    for e in strong_events:
        qualifying_tids = [t for t in e["top_tids"] if tid_prior_count[t] >= PRIOR_COUNT_MIN]
        if qualifying_tids and e["next_date"] and e["next_open"] and e["next_close"]:
            fut = resolve_futures_symbol(e["stock_id"], e["next_date"])
            tradable = fut is not None
            gap = (e["next_open"] - e["close"]) / e["close"] * 100
            intraday = (e["next_close"] - e["next_open"]) / e["next_open"] * 100
            actionable.append({
                "stock_id": e["stock_id"], "date": e["date"], "next_date": e["next_date"],
                "qualifying_tids": qualifying_tids, "tradable_futures": tradable,
                "gap_pct": gap, "intraday_pct": intraday,
            })
        for t in e["top_tids"]:
            tid_prior_count[t] += 1

    branch_count = defaultdict(int)
    for e in strong_events:
        for t in e["top_tids"]:
            branch_count[t] += 1
    repeat_branches = {t: n for t, n in branch_count.items() if n >= 4}
    print(f"top3淨賣超且全期重複>=4次的分點數: {len(repeat_branches)}")
    for tid, n in sorted(repeat_branches.items(), key=lambda x: -x[1])[:15]:
        print(f"  {tid}: {n} 次")

    spec_path = (
        stock_db.PROJECT_ROOT
        / "reports/research/branch-footprint-screen/dayflip_gapup_short/FROZEN_SPEC_V1.json"
    )
    spec = json.loads(spec_path.read_text())
    existing_tids = set(dict(spec["seat_flip_table_frozen"]["values"]).keys())
    overlap = set(repeat_branches) & existing_tids
    print(f"與現行dayflip-short 24席重疊: {sorted(overlap)}")

    print(f"\nPIT-safe合格事件（分點已有>={PRIOR_COUNT_MIN}次前科）: {len(actionable)}")
    tradable_events = [a for a in actionable if a["tradable_futures"]]
    print(f"其中隔天有可交易股票期貨代碼: {len(tradable_events)}")

    import statistics as st

    def report(label: str, events: list[dict]) -> None:
        if not events:
            print(f"{label}: n=0")
            return
        vals = [e["intraday_pct"] for e in events]
        gaps = [e["gap_pct"] for e in events]
        n = len(vals)
        win_long = sum(1 for x in vals if x > 0) / n * 100
        p10 = sorted(vals)[int(n * 0.1)]
        p90 = sorted(vals)[min(n - 1, int(n * 0.9))]
        worst5 = sorted(vals)[:5]
        best5 = sorted(vals)[-5:]
        print(f"{label}: n={n}  跳空%均={st.mean(gaps):+.2f}  "
              f"次日開盤->收盤%均={st.mean(vals):+.2f} 中位={st.median(vals):+.2f} "
              f"標準差={st.pstdev(vals):.2f}  做多(反彈)勝率={win_long:.1f}%  "
              f"p10={p10:+.2f} p90={p90:+.2f}")
        print(f"    最差5筆(對做多而言): {[round(x,1) for x in worst5]}")
        print(f"    最佳5筆(對做多而言): {[round(x,1) for x in best5]}")

    print("\n=== 次日開盤進場->次日收盤出場 ===")
    report("PIT-safe全部", actionable)
    report("PIT-safe+可交易期貨", tradable_events)

    if tradable_events:
        mid = tradable_events[len(tradable_events) // 2]["date"]
        first_half = [e for e in tradable_events if e["date"] < mid]
        second_half = [e for e in tradable_events if e["date"] >= mid]
        print(f"\n=== Walk-forward（分界日 {mid}）===")
        report("前半段", first_half)
        report("後半段", second_half)

    out_path = (
        stock_db.PROJECT_ROOT
        / "reports/research/branch-footprint-screen/dayflip_gapup_short/limit_down_locker_walkforward.json"
    )
    out_path.write_text(json.dumps({
        "start_date": START_DATE,
        "prior_count_min": PRIOR_COUNT_MIN,
        "n_strong_events": len(strong_events),
        "n_distinct_stocks": n_stocks,
        "repeat_branches": repeat_branches,
        "overlap_with_existing_24": sorted(overlap),
        "n_pit_safe_actionable": len(actionable),
        "n_tradable": len(tradable_events),
        "events": tradable_events,
    }, ensure_ascii=False, indent=2))
    print(f"\n寫入: {out_path}")


if __name__ == "__main__":
    main()
