"""limit_up_locker_walkforward_verify.py 的後續：幫230筆PIT-safe+可交易期貨事件
加停損規則，重算風報比；並用1分K交叉驗證日K版本的保守假設方向對不對。

已知限制（誠實記錄）：
  - 日K版本的停損模擬用「保守排序」假設——如果當天最低點跌破停損價，一律假設
    停損先被觸發（不管盤中實際上是先跌破停損還是先衝高再跌回），這是沒有分K
    資料時唯一不會偷看未來/不會過度樂觀的做法，但可能低估報酬（如果實際上是
    先衝高、觸及停損前你早就想獲利了結）。
  - stock_kbar_1m只涵蓋175檔股票（既有策略追蹤的池子），230筆事件裡只有67筆
    (29%)有>=50根1分K可查——這67筆拿來做真1分K停損模擬當交叉驗證，其餘161筆
    只能用日K保守假設版本。67筆子集可能偏向「已經在追蹤池子裡的相對大型股」，
    不能完全代表全部230筆的股票組成，只能當方向性交叉驗證，不是完整覆蓋。
"""

from __future__ import annotations

import json
import sqlite3
import statistics as st

import stock_db

DB_PATH = stock_db.DEFAULT_DB_PATH
STOP_PCTS = [0.02, 0.03, 0.05]


def load_events() -> list[dict]:
    p = (
        stock_db.PROJECT_ROOT
        / "reports/research/branch-footprint-screen/dayflip_gapup_short/limit_up_locker_walkforward.json"
    )
    return json.loads(p.read_text())["events"]


def main() -> None:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    events = load_events()

    # 補回 next_open/high/low/close（walkforward.json只存了gap_pct/intraday_pct）
    for e in events:
        row = con.execute(
            "SELECT open, high, low, close FROM stock_daily_bars "
            "WHERE source='finmind' AND stock_id=? AND trade_date=?",
            (e["stock_id"], e["next_date"]),
        ).fetchone()
        e["next_open"], e["next_high"], e["next_low"], e["next_close"] = row
        e["intraday_pct"] = (e["next_close"] - e["next_open"]) / e["next_open"] * 100

    def daily_stop_sim(e: dict, stop_pct: float) -> float:
        entry = e["next_open"]
        stop_px = entry * (1 - stop_pct)
        if e["next_low"] <= stop_px:
            return (stop_px - entry) / entry * 100  # 保守假設：停損先觸發
        return (e["next_close"] - entry) / entry * 100

    def report(label: str, vals: list[float]) -> None:
        n = len(vals)
        if n == 0:
            print(f"{label}: n=0")
            return
        win = sum(1 for x in vals if x > 0) / n * 100
        worst5 = sorted(vals)[:5]
        print(f"{label}: n={n}  均={st.mean(vals):+.2f}%  中位={st.median(vals):+.2f}%  "
              f"標準差={st.pstdev(vals):.2f}  勝率={win:.1f}%  最差5筆={[round(x,1) for x in worst5]}")

    baseline = [e["intraday_pct"] for e in events]
    print("=== 全部230筆：無停損 baseline ===")
    report("無停損", baseline)

    print("\n=== 全部230筆：日K保守假設停損模擬 ===")
    for sp in STOP_PCTS:
        vals = [daily_stop_sim(e, sp) for e in events]
        report(f"停損-{sp*100:.0f}%", vals)

    # 1分K交叉驗證子集
    sub_events = []
    for e in events:
        n_bars = con.execute(
            "SELECT COUNT(*) FROM stock_kbar_1m WHERE stock_id=? AND trade_date=?",
            (e["stock_id"], e["next_date"]),
        ).fetchone()[0]
        if n_bars >= 50:
            sub_events.append(e)
    print(f"\n=== 1分K交叉驗證子集: n={len(sub_events)} ===")

    def true_1m_stop_sim(e: dict, stop_pct: float) -> float | None:
        bars = con.execute(
            "SELECT minute, open, high, low, close FROM stock_kbar_1m "
            "WHERE stock_id=? AND trade_date=? ORDER BY minute",
            (e["stock_id"], e["next_date"]),
        ).fetchall()
        if not bars:
            return None
        entry = float(bars[0][1])  # 第一根K棒開盤價 = 當天開盤價
        stop_px = entry * (1 - stop_pct)
        exit_px = float(bars[-1][4])  # fallback: 收盤
        for _t, _o, h, l, c in bars:
            if float(l) <= stop_px:
                exit_px = stop_px
                break
        return (exit_px - entry) / entry * 100

    sub_baseline = [e["intraday_pct"] for e in sub_events]
    print("[子集] 無停損 baseline:")
    report("無停損", sub_baseline)
    print("[子集] 日K保守假設版本:")
    for sp in STOP_PCTS:
        vals = [daily_stop_sim(e, sp) for e in sub_events]
        report(f"停損-{sp*100:.0f}%(日K版)", vals)
    print("[子集] 真1分K版本（精確路徑，無保守假設誤差）:")
    for sp in STOP_PCTS:
        vals = [v for v in (true_1m_stop_sim(e, sp) for e in sub_events) if v is not None]
        report(f"停損-{sp*100:.0f}%(1分K版)", vals)


if __name__ == "__main__":
    main()
