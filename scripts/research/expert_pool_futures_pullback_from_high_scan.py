#!/usr/bin/env python3
"""分點專家池個股期貨「相對當日高點回檔X% 進場 + 移動停利」掃描（exploratory · 非採納）.

跟 expert_pool_futures_dip_buy_trail_scan.py（相對前一日收盤）不同的進場基準：
  - 進場：截至目前 tick 為止的「當日累積最高價」（causal running high，不是全天
    完整最高價——用完整最高價回頭比較會犯 look-ahead，見
    docs/research-integrity-checklist.md BUG-2）× (1 - dip%)；價格回落碰到這個
    會隨每個新高不斷上移的門檻，視為成交，成交價＝門檻本身（不取更有利的實際
    成交價，避免 BUG-5 fill clamp）。
  - 出場：跟前一版一樣，進場後追蹤高點，跌破 高點×(1-trail%) 觸發移動停利；
    當日結束前沒觸發就強制在最後一筆 tick 平倉（當天平倉，日內策略）。
  - 完全自足於單一交易日，不需要前一日收盤價，因此不受轉倉日影響、樣本天數
    比前一版（skip 轉倉日）多。

限制同前一版腳本：22天樣本、不含手續費滑價、grid search 多重比較。

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/expert_pool_futures_pullback_from_high_scan.py
"""

from __future__ import annotations

import math
import statistics
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from expert_pool_futures_dip_buy_trail_scan import DATA_DIR, load_stock  # noqa: E402

DIPS = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
TRAILS = [1.0, 1.5, 2.0, 3.0, 5.0]


def simulate_day(prices: np.ndarray, dip_pct: float, trail_pct: float) -> dict | None:
    if prices.size < 2:
        return None
    running_high = np.maximum.accumulate(prices)
    threshold = running_high * (1 - dip_pct / 100.0)
    touch = np.where(prices <= threshold)[0]
    if touch.size == 0:
        return None
    entry_idx = int(touch[0])
    fill = float(threshold[entry_idx])
    remainder = prices[entry_idx + 1 :]
    if remainder.size == 0:
        return {"entry": fill, "exit": fill, "ret_pct": 0.0, "reason": "no_ticks_after_entry"}
    post_peak = np.maximum.accumulate(np.concatenate(([fill], remainder)))[1:]
    stops = post_peak * (1 - trail_pct / 100.0)
    breach = np.where(remainder <= stops)[0]
    if breach.size > 0:
        exit_price = float(stops[int(breach[0])])
        reason = "trail_stop"
    else:
        exit_price = float(remainder[-1])
        reason = "day_end_forced"
    ret_pct = (exit_price - fill) / fill * 100.0
    return {"entry": fill, "exit": exit_price, "ret_pct": ret_pct, "reason": reason}


def simulate(days: dict[str, tuple[np.ndarray, str]], dip_pct: float, trail_pct: float) -> list[dict]:
    trades = []
    for d in sorted(days.keys()):
        prices, _contract = days[d]
        res = simulate_day(prices, dip_pct, trail_pct)
        if res:
            res["date"] = d
            trades.append(res)
    return trades


def naive_ttest(returns: list[float]) -> tuple[float, float]:
    n = len(returns)
    if n < 2:
        return float("nan"), float("nan")
    mean = statistics.mean(returns)
    sd = statistics.stdev(returns)
    if sd == 0:
        return float("nan"), float("nan")
    t = mean / (sd / math.sqrt(n))
    from math import erf

    p = 2 * (1 - 0.5 * (1 + erf(abs(t) / math.sqrt(2))))
    return t, p


def main() -> int:
    files = sorted(DATA_DIR.glob("*_tick_*.csv"))
    print(f"標的數：{len(files)}")

    per_stock_days: dict[str, dict] = {}
    for path in files:
        sid = path.name.split("_")[0]
        per_stock_days[sid] = load_stock(path)

    print(f"\n{'dip%':>5} {'trail%':>6} {'n_trades':>8} {'win%':>6} {'mean_ret%':>10} {'median%':>9} {'sum%':>8} {'t':>6} {'p':>6}")
    rows = []
    grid_pooled: dict[tuple[float, float], list[dict]] = {}
    for dip in DIPS:
        for trail in TRAILS:
            pooled = []
            for sid, days in per_stock_days.items():
                for t in simulate(days, dip, trail):
                    t["stock_id"] = sid
                    pooled.append(t)
            grid_pooled[(dip, trail)] = pooled
            rets = [t["ret_pct"] for t in pooled]
            n = len(rets)
            if n == 0:
                rows.append((dip, trail, 0, float("nan"), float("nan"), float("nan"), 0.0, float("nan"), float("nan")))
                continue
            win = sum(1 for r in rets if r > 0) / n * 100
            mean_r = statistics.mean(rets)
            median_r = statistics.median(rets)
            sum_r = sum(rets)
            t_stat, p_val = naive_ttest(rets)
            rows.append((dip, trail, n, win, mean_r, median_r, sum_r, t_stat, p_val))
            print(
                f"{dip:>5.1f} {trail:>6.1f} {n:>8} {win:>6.1f} {mean_r:>10.3f} {median_r:>9.3f} "
                f"{sum_r:>8.1f} {t_stat:>6.2f} {p_val:>6.3f}"
            )

    n_combos = len(DIPS) * len(TRAILS)
    print(f"\n共 {n_combos} 組合 · 純雜訊下 p<0.05 預期約 {n_combos*0.05:.1f} 組會通過")

    MIN_N = 30
    ranked = [r for r in rows if r[2] >= MIN_N]
    ranked.sort(key=lambda r: r[4], reverse=True)
    print(f"\n=== 平均報酬%最高（n≥{MIN_N}）前5 ===")
    for dip, trail, n, win, mean_r, median_r, sum_r, t_stat, p_val in ranked[:5]:
        print(f"  dip={dip}% trail={trail}%: n={n} win={win:.1f}% mean={mean_r:.3f}% t={t_stat:.2f} p={p_val:.3f}")

    print(f"\n=== 平均報酬%最低（n≥{MIN_N}）後5 ===")
    for dip, trail, n, win, mean_r, median_r, sum_r, t_stat, p_val in ranked[-5:]:
        print(f"  dip={dip}% trail={trail}%: n={n} win={win:.1f}% mean={mean_r:.3f}% t={t_stat:.2f} p={p_val:.3f}")

    best_dip, best_trail = ranked[0][0], ranked[0][1]
    print(f"\n=== 逐檔明細 dip={best_dip}% trail={best_trail}%（全域最佳格）===")
    print(f"{'stock':>6} {'n':>4} {'win%':>6} {'mean%':>8} {'sum%':>8}")
    for sid, days in per_stock_days.items():
        trades = simulate(days, best_dip, best_trail)
        if not trades:
            print(f"{sid:>6} {0:>4} {'--':>6} {'--':>8} {'--':>8}")
            continue
        rets = [t["ret_pct"] for t in trades]
        win = sum(1 for r in rets if r > 0) / len(rets) * 100
        print(f"{sid:>6} {len(rets):>4} {win:>6.1f} {statistics.mean(rets):>8.3f} {sum(rets):>8.1f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
