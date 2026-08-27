#!/usr/bin/env python3
"""開盤動能延續：不分專家池/對照組，67檔合併找名單與條件.

沿用 expert_pool_futures_open_breakout_scan.py 驗證過的策略骨架
（開盤價±0.5%突破判斷多空、trail=1%移動停利、當天平倉），固定在這個「旗艦格」
上，換個角度分析：

  1. 逐檔排名：哪些股票的動能延續效應最強，跟流動性（日均tick數）的關係
  2. 跟前一日收盤的關係：今天開盤跳空方向 vs 動能方向是否一致（追缺口 vs 補缺口）
  3. 跟大盤的關係：日層級平均報酬 vs 台指期(TX)當日漲跌幅／振幅
  4. 使用時機：進場觸發點落在盤中哪個時段

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/momentum_continuation_universe_scan.py
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from expert_pool_futures_dip_buy_trail_scan import DATA_DIR, load_stock  # noqa: E402
from expert_pool_futures_open_breakout_scan import simulate_day  # noqa: E402

X_PCT = 0.5
TRAIL_PCT = 1.0


def load_all_files() -> dict[str, dict[str, tuple[np.ndarray, str]]]:
    """sid -> {date: (prices, contract)}，IS+OOS 兩期間合併（跨期間日期不重疊）."""
    per_stock: dict[str, dict] = {}
    for path in sorted(DATA_DIR.glob("*.csv")):
        name = path.name
        if name.startswith("ctrl_"):
            sid = name.split("_")[1]
        else:
            sid = name.split("_")[0]
        days = load_stock(path)
        per_stock.setdefault(sid, {}).update(days)
    return per_stock


def main() -> int:
    per_stock = load_all_files()
    print(f"合併後標的數：{len(per_stock)}")

    tx_is = json.loads(Path("/tmp/tx_is.json").read_text())
    tx_oos = json.loads(Path("/tmp/tx_oos.json").read_text())
    tx = {**tx_is, **tx_oos}
    print(f"TX 大盤代理天數：{len(tx)}")

    # === per-stock ranking + liquidity ===
    per_stock_rows = []
    all_trades = []  # each: dict with sid, date, direction, ret_pct, gap_pct, entry_frac, mkt_ret, reason
    for sid, days in per_stock.items():
        dates = sorted(days.keys())
        prev_close = None
        n_ticks_list = []
        trades = []
        for d in dates:
            prices, contract = days[d]
            n_ticks_list.append(prices.size)
            res = simulate_day(prices, X_PCT, TRAIL_PCT)
            if res:
                open_price = float(prices[0])
                gap_pct = None
                if prev_close is not None:
                    gap_pct = (open_price - prev_close) / prev_close * 100
                mkt = tx.get(d)
                mkt_ret = None
                if mkt:
                    mkt_ret = (mkt["close"] - mkt["open"]) / mkt["open"] * 100
                trades.append(
                    {
                        "sid": sid,
                        "date": d,
                        "direction": res["direction"],
                        "ret_pct": res["ret_pct"],
                        "reason": res["reason"],
                        "gap_pct": gap_pct,
                        "mkt_ret": mkt_ret,
                    }
                )
            prev_close = float(prices[-1])
        all_trades.extend(trades)
        rets = [t["ret_pct"] for t in trades]
        if rets:
            avg_ticks = statistics.mean(n_ticks_list)
            per_stock_rows.append(
                {
                    "sid": sid,
                    "n": len(rets),
                    "win": sum(1 for r in rets if r > 0) / len(rets) * 100,
                    "mean": statistics.mean(rets),
                    "sum": sum(rets),
                    "avg_ticks_per_day": avg_ticks,
                }
            )

    per_stock_rows.sort(key=lambda r: r["mean"], reverse=True)
    print(f"\n=== 逐檔排名（x={X_PCT}% trail={TRAIL_PCT}%，IS+OOS合併，{len(per_stock_rows)}檔）===")
    print(f"{'sid':>6} {'n':>4} {'win%':>6} {'mean%':>8} {'sum%':>8} {'avg_ticks/day':>14}")
    for r in per_stock_rows:
        print(f"{r['sid']:>6} {r['n']:>4} {r['win']:>6.1f} {r['mean']:>8.3f} {r['sum']:>8.1f} {r['avg_ticks_per_day']:>14.0f}")

    # liquidity vs performance correlation
    ticks = [r["avg_ticks_per_day"] for r in per_stock_rows]
    means = [r["mean"] for r in per_stock_rows]
    if len(ticks) > 2:
        corr = np.corrcoef(np.log(np.array(ticks) + 1), np.array(means))[0, 1]
        print(f"\nlog(日均tick數) vs 平均報酬% 相關係數：{corr:.3f}")

    # === gap relationship ===
    gap_trades = [t for t in all_trades if t["gap_pct"] is not None]
    print(f"\n=== 跟前一日收盤的關係（開盤跳空 vs 動能方向）n={len(gap_trades)} ===")
    same_dir = []  # breakout direction agrees with gap direction (gap up -> long, or gap down -> short)
    opp_dir = []
    flat_gap = []  # |gap| very small, direction ambiguous
    for t in gap_trades:
        g = t["gap_pct"]
        d = t["direction"]
        if abs(g) < 0.05:
            flat_gap.append(t)
            continue
        gap_dir = "long" if g > 0 else "short"
        if gap_dir == d:
            same_dir.append(t)
        else:
            opp_dir.append(t)

    def summarize(label, lst):
        if not lst:
            print(f"  {label}: n=0")
            return
        rets = [t["ret_pct"] for t in lst]
        win = sum(1 for r in rets if r > 0) / len(rets) * 100
        print(f"  {label}: n={len(lst)} win={win:.1f}% mean={statistics.mean(rets):.3f}% sum={sum(rets):.1f}%")

    summarize("開盤跳空方向 == 動能方向（追缺口，例如跳空漲、也做多）", same_dir)
    summarize("開盤跳空方向 != 動能方向（補缺口後才反向動能，例如跳空漲、卻做空）", opp_dir)
    summarize("開盤跳空 <0.05%（幾乎無缺口）", flat_gap)

    # bucket by |gap| size
    print("\n  依跳空幅度分桶：")
    buckets = [(0, 0.1), (0.1, 0.3), (0.3, 0.6), (0.6, 999)]
    for lo, hi in buckets:
        lst = [t for t in gap_trades if lo <= abs(t["gap_pct"]) < hi]
        summarize(f"  |gap| in [{lo}%, {hi}%)", lst)

    # === market relationship ===
    print(f"\n=== 跟大盤(TX)的關係 ===")
    by_date = {}
    for t in all_trades:
        by_date.setdefault(t["date"], []).append(t["ret_pct"])
    day_rows = []
    for d, rets in by_date.items():
        mkt = tx.get(d)
        if not mkt:
            continue
        mkt_ret = (mkt["close"] - mkt["open"]) / mkt["open"] * 100
        day_rows.append({"date": d, "day_mean_strategy_ret": statistics.mean(rets), "mkt_ret": mkt_ret, "n": len(rets)})

    mkt_rets = np.array([r["mkt_ret"] for r in day_rows])
    strat_rets = np.array([r["day_mean_strategy_ret"] for r in day_rows])
    corr_dir = np.corrcoef(mkt_rets, strat_rets)[0, 1]
    corr_abs = np.corrcoef(np.abs(mkt_rets), strat_rets)[0, 1]
    print(f"  n_days={len(day_rows)}")
    print(f"  日均策略報酬 vs TX當日漲跌幅 相關係數：{corr_dir:.3f}（>0代表大盤漲時策略也表現較好）")
    print(f"  日均策略報酬 vs |TX當日漲跌幅|（振幅代理）相關係數：{corr_abs:.3f}（>0代表大盤震盪越大策略越賺）")

    # split: big TX move days vs calm days
    abs_mkt = np.abs(mkt_rets)
    median_abs = float(np.median(abs_mkt))
    big_days = [r for r, m in zip(day_rows, abs_mkt) if m >= median_abs]
    calm_days = [r for r, m in zip(day_rows, abs_mkt) if m < median_abs]
    print(f"  TX振幅前50%大的日子（n={len(big_days)}）：策略日均報酬 mean={statistics.mean(r['day_mean_strategy_ret'] for r in big_days):.3f}%")
    print(f"  TX振幅後50%小的日子（n={len(calm_days)}）：策略日均報酬 mean={statistics.mean(r['day_mean_strategy_ret'] for r in calm_days):.3f}%")

    # === timing: entry position within the day ===
    print(f"\n=== 使用時機：進場觸發點落在當日第幾筆tick（相對位置）===")
    entry_fracs = []
    for sid, days in per_stock.items():
        for d, (prices, contract) in days.items():
            if prices.size < 2:
                continue
            running_high = np.maximum.accumulate(prices)
            threshold_long = running_high * (1 - 999)  # placeholder unused
    # simpler: recompute using simulate_day but capture entry_idx fraction
    for sid, days in per_stock.items():
        for d in sorted(days.keys()):
            prices, contract = days[d]
            if prices.size < 2:
                continue
            open_price = float(prices[0])
            long_trigger = open_price * (1 + X_PCT / 100.0)
            short_trigger = open_price * (1 - X_PCT / 100.0)
            long_hits = np.where(prices >= long_trigger)[0]
            short_hits = np.where(prices <= short_trigger)[0]
            long_idx = int(long_hits[0]) if long_hits.size else None
            short_idx = int(short_hits[0]) if short_hits.size else None
            if long_idx is None and short_idx is None:
                continue
            if short_idx is None or (long_idx is not None and long_idx < short_idx):
                entry_idx = long_idx
            else:
                entry_idx = short_idx
            entry_fracs.append(entry_idx / prices.size)

    entry_fracs = np.array(entry_fracs)
    print(f"  n={len(entry_fracs)}")
    print(f"  進場位置佔當日tick序列比例：mean={entry_fracs.mean():.3f} median={np.median(entry_fracs):.3f}")
    for pct in (10, 25, 50, 75, 90):
        print(f"    p{pct}: {np.percentile(entry_fracs, pct):.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
