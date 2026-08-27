"""隔日沖鎖漲停分點簽名掃描（探索性研究，非production程式碼）。

使用者假說：有些分點專門把股票『鎖』在漲停（不只是漲停，是一價到底式的鎖死，
盤中幾乎沒有像樣的成交量讓人買到），這種鎖法通常是隔日出貨的前兆——隔天做空
可能有 edge。想知道：
  1) 這種「鎖漲停」事件裡，是否有分點重複出現（可辨識的『鎖手』簽名）？
  2) 這些分點是否已經在 dayflip-short 現有的24席名單裡（FROZEN_SPEC_V1.json）？
  3) 被這些分點鎖過漲停的股票，隔天報酬分布如何——做空有沒有 edge？

資料限制：stock_broker_branch_daily 只從 2024-07-01 開始（約2年），比
dayflip-short 原始研究窗口短很多，這裡的結論只能當初步線索，不是可以直接
上線的規格。

漲停價計算：沿用台股跳動點位表（<10:0.01,10-50:0.05,50-100:0.1,100-500:0.5,
500-1000:1,>=1000:5），無條件捨去到跳動點位（不超過理論10%上限）。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

import stock_db

DB_PATH = stock_db.DEFAULT_DB_PATH
START_DATE = "2024-07-01"


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


def limit_up_price(prev_close: float) -> float:
    theo = prev_close * 1.10
    ts = tick_size(theo)
    n = int(round(theo / ts + 1e-9))
    # 無條件捨去到跳動點位（不能超過理論10%上限）
    while n * ts > theo + 1e-6:
        n -= 1
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

    # step1: 找「鎖漲停」事件 = 收盤==理論漲停價 AND 當天低點也很接近漲停（一價到底）
    lock_events: list[dict] = []
    for sid, series in by_stock.items():
        if sid.startswith("00"):
            continue  # 排除ETF
        for i in range(1, len(series)):
            d, o, h, l, c = series[i]
            prev_c = series[i - 1][4]
            if prev_c <= 0:
                continue
            lim = limit_up_price(prev_c)
            if abs(c - lim) > 0.011:
                continue
            strong_lock = l >= lim - 1e-6  # 全天沒跌破漲停 = 一價到底式鎖死
            lock_events.append(
                {"stock_id": sid, "date": d, "prev_close": prev_c, "limit_up": lim,
                 "open": o, "high": h, "low": l, "close": c, "strong_lock": strong_lock,
                 "next_date": series[i + 1][0] if i + 1 < len(series) else None,
                 "next_open": series[i + 1][1] if i + 1 < len(series) else None,
                 "next_close": series[i + 1][4] if i + 1 < len(series) else None}
            )

    n_all = len(lock_events)
    n_strong = sum(1 for e in lock_events if e["strong_lock"])
    print(f"鎖漲停事件（2024-07-01起）：全部 {n_all} 筆，其中一價到底式強鎖 {n_strong} 筆")

    strong_events = [e for e in lock_events if e["strong_lock"]]

    # step2: 對每個強鎖事件查當天分點買超，找主要買方
    MIN_BUY_NTD = 3_000_000
    branch_event_count: dict[str, int] = defaultdict(int)
    branch_events: dict[str, list[dict]] = defaultdict(list)

    for e in strong_events:
        sid, d = e["stock_id"], e["date"]
        buys = con.execute(
            "SELECT securities_trader_id, buy, sell FROM stock_broker_branch_daily "
            "WHERE stock_id=? AND trade_date=?", (sid, d),
        ).fetchall()
        if not buys:
            continue
        px = e["close"]
        ranked = []
        for tid, b, s in buys:
            net_amt = (float(b or 0) - float(s or 0)) * px
            if net_amt >= MIN_BUY_NTD:
                ranked.append((tid, net_amt))
        ranked.sort(key=lambda x: -x[1])
        top = ranked[:3]
        for tid, amt in top:
            branch_event_count[tid] += 1
            branch_events[tid].append({**e, "net_buy_ntd": amt})

    repeat_branches = {tid: n for tid, n in branch_event_count.items() if n >= 4}
    repeat_branches = dict(sorted(repeat_branches.items(), key=lambda x: -x[1]))

    print(f"\n強鎖事件中可查到分點買超資料的筆數：{sum(1 for e in strong_events)}")
    print(f"出現在 top3 買方且重複 >=4 次的分點數：{len(repeat_branches)}")
    for tid, n in list(repeat_branches.items())[:20]:
        print(f"  {tid}: {n} 次")

    # step3: 現有dayflip-short 24席名單比對
    spec_path = (
        stock_db.PROJECT_ROOT
        / "reports/research/branch-footprint-screen/dayflip_gapup_short/FROZEN_SPEC_V1.json"
    )
    spec = json.loads(spec_path.read_text())
    existing_tids = set(dict(spec["seat_flip_table_frozen"]["values"]).keys())
    overlap = set(repeat_branches) & existing_tids
    print(f"\n現有dayflip-short 24席名單: {len(existing_tids)} 席")
    print(f"重複鎖漲停分點與現有名單重疊: {sorted(overlap)}")
    print(f"重複鎖漲停分點但『不在』現有24席名單: {sorted(set(repeat_branches) - existing_tids)}")

    # step4: 對repeat branches鎖過的事件，算隔天報酬（做空角度：跌=正報酬）
    all_next_day = []
    for tid in repeat_branches:
        for e in branch_events[tid]:
            if e["next_open"] is None or e["next_close"] is None:
                continue
            gap = (e["next_open"] - e["close"]) / e["close"] * 100
            intraday = (e["next_close"] - e["next_open"]) / e["next_open"] * 100
            full = (e["next_close"] - e["close"]) / e["close"] * 100
            all_next_day.append({"tid": tid, "stock_id": e["stock_id"], "date": e["date"],
                                  "gap_pct": gap, "intraday_pct": intraday, "full_day_pct": full})

    if all_next_day:
        import statistics as st
        gaps = [x["gap_pct"] for x in all_next_day]
        fulls = [x["full_day_pct"] for x in all_next_day]
        n = len(all_next_day)
        win_short = sum(1 for x in fulls if x < 0) / n * 100
        print(f"\n=== 重複鎖漲停分點事件的隔天報酬（n={n}）===")
        print(f"隔天跳空% 平均: {st.mean(gaps):+.2f}  中位數: {st.median(gaps):+.2f}")
        print(f"隔天全天報酬%(收盤vs前收) 平均: {st.mean(fulls):+.2f}  中位數: {st.median(fulls):+.2f}  標準差: {st.pstdev(fulls):.2f}")
        print(f"做空(隔天收跌)勝率: {win_short:.1f}%")

    out_path = (
        stock_db.PROJECT_ROOT
        / "reports/research/branch-footprint-screen/dayflip_gapup_short/limit_up_locker_scan.json"
    )
    out_path.write_text(json.dumps({
        "start_date": START_DATE,
        "n_lock_events_all": n_all,
        "n_lock_events_strong": n_strong,
        "repeat_branches": repeat_branches,
        "overlap_with_existing_24": sorted(overlap),
        "new_branches_not_in_existing": sorted(set(repeat_branches) - existing_tids),
        "next_day_stats_n": len(all_next_day),
    }, ensure_ascii=False, indent=2))
    print(f"\n寫入: {out_path}")


if __name__ == "__main__":
    main()
