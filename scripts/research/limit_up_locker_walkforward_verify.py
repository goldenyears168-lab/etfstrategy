"""limit_up_locker_branch_scan.py 初步發現的PIT-safe＋流動性過濾＋walk-forward驗證。

初版掃描（limit_up_locker_branch_scan.py）有一個沒講清楚的look-ahead：判定
「哪些分點是重複鎖手」用的是全部2年資料的次數（>=4次），這本身就偷看了未來——
真實下單當下，你不會知道某分點『之後』還會再鎖幾次。這裡改成PIT-safe版本：
在每個事件當下，只用『這個分點在這個日期之前』的鎖漲停次數來判斷夠不夠格
（>=3次前科，等於是第4次以上出手才算數），跟這個session稍早修的
seat_flip PIT bug同一個精神。

另外加兩項現實可執行性檢查：
  1) 隔天(next_date)是否有可交易的股票期貨代碼（resolve_futures_symbol）——
     沒有代碼就算算出報酬再好也不能真的做這筆交易。
  2) walk-forward：前後兩段時間分開算，看訊號在後段(out-of-sample相對於前段
     學到的分點名單)是否還成立，而不是整段時間混著算一個平均數字。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

import stock_db
from order.dayflip_short_signal import resolve_futures_symbol

DB_PATH = stock_db.DEFAULT_DB_PATH
START_DATE = "2024-07-01"
MIN_BUY_NTD = 3_000_000
PRIOR_COUNT_MIN = 3  # 第4次以上出手才算「已建立track record」


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

    strong_events = []
    for sid, series in by_stock.items():
        if sid.startswith("00"):
            continue
        for i in range(1, len(series)):
            d, o, h, l, c = series[i]
            prev_c = series[i - 1][4]
            if prev_c <= 0:
                continue
            lim = limit_up_price(prev_c)
            if abs(c - lim) > 0.011:
                continue
            if l >= lim - 1e-6:
                strong_events.append({
                    "stock_id": sid, "date": d, "close": c,
                    "next_date": series[i + 1][0] if i + 1 < len(series) else None,
                    "next_open": series[i + 1][1] if i + 1 < len(series) else None,
                    "next_close": series[i + 1][4] if i + 1 < len(series) else None,
                })

    strong_events.sort(key=lambda e: e["date"])
    print(f"強鎖事件總數: {len(strong_events)}")

    # 每個事件找top3買方分點
    for e in strong_events:
        buys = con.execute(
            "SELECT securities_trader_id, buy, sell FROM stock_broker_branch_daily "
            "WHERE stock_id=? AND trade_date=?", (e["stock_id"], e["date"]),
        ).fetchall()
        px = e["close"]
        ranked = []
        for tid, b, s in buys:
            net_amt = (float(b or 0) - float(s or 0)) * px
            if net_amt >= MIN_BUY_NTD:
                ranked.append((tid, net_amt))
        ranked.sort(key=lambda x: -x[1])
        e["top_tids"] = [tid for tid, _ in ranked[:3]]

    # PIT-safe：逐日走，維護每個tid截至目前(不含今天)的鎖漲停出現次數
    tid_prior_count: dict[str, int] = defaultdict(int)
    actionable = []  # PIT-safe合格事件（分點在此之前已有>=PRIOR_COUNT_MIN次前科）
    for e in strong_events:
        qualifying_tids = [t for t in e["top_tids"] if tid_prior_count[t] >= PRIOR_COUNT_MIN]
        if qualifying_tids and e["next_date"] and e["next_open"] and e["next_close"]:
            fut = resolve_futures_symbol(e["stock_id"], e["next_date"])
            tradable = fut is not None
            full_day = (e["next_close"] - e["close"]) / e["close"] * 100
            gap = (e["next_open"] - e["close"]) / e["close"] * 100
            actionable.append({
                "stock_id": e["stock_id"], "date": e["date"], "next_date": e["next_date"],
                "qualifying_tids": qualifying_tids, "tradable_futures": tradable,
                "gap_pct": gap, "full_day_pct": full_day,
            })
        for t in e["top_tids"]:
            tid_prior_count[t] += 1

    print(f"PIT-safe合格事件（分點已有>={PRIOR_COUNT_MIN}次前科）: {len(actionable)}")
    tradable_events = [a for a in actionable if a["tradable_futures"]]
    print(f"其中隔天有可交易股票期貨代碼: {len(tradable_events)}")

    import statistics as st

    def report(label: str, events: list[dict]) -> None:
        if not events:
            print(f"{label}: n=0，無法算")
            return
        fulls = [e["full_day_pct"] for e in events]
        gaps = [e["gap_pct"] for e in events]
        n = len(events)
        win_long = sum(1 for x in fulls if x > 0) / n * 100
        p10 = sorted(fulls)[int(n * 0.1)]
        p90 = sorted(fulls)[min(n - 1, int(n * 0.9))]
        print(f"{label}: n={n}  跳空%均={st.mean(gaps):+.2f}  "
              f"全天%均={st.mean(fulls):+.2f} 中位={st.median(fulls):+.2f} "
              f"標準差={st.pstdev(fulls):.2f}  做多勝率={win_long:.1f}%  "
              f"p10={p10:+.2f} p90={p90:+.2f}")

    print("\n=== 全樣本 vs 流動性過濾後 ===")
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
        / "reports/research/branch-footprint-screen/dayflip_gapup_short/limit_up_locker_walkforward.json"
    )
    out_path.write_text(json.dumps({
        "start_date": START_DATE,
        "prior_count_min": PRIOR_COUNT_MIN,
        "n_strong_events": len(strong_events),
        "n_pit_safe_actionable": len(actionable),
        "n_tradable": len(tradable_events),
        "events": tradable_events,
    }, ensure_ascii=False, indent=2))
    print(f"\n寫入: {out_path}")


if __name__ == "__main__":
    main()
