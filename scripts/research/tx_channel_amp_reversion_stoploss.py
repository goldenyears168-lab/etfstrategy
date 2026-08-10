"""Ad-hoc round 13: add a hard stop-loss to the round-11 "hold until reversion"
strategy (which loses because a minority of trend days never revert and run
up unbounded losses before forced exit). Also tests whether a causal
trend-strength read at entry can be used to skip or flip (fade->follow) the
trade instead of just capping losses.

Exit priority per bar: stop-loss (price moved against entry by STOP*amp) >
touch (price reached entry-time center) > max_hold timeout > end of day.

Uses the corrected ticks_to_1m_bars. Not wired into any pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np

from tx_channel_amp_volume_interaction import load_all_day_bars, weighted_amp30
from tx_channel_amp_persistence_drivers import day_clustered, slope30
from tx_channel_slope_persistence_140d import rolling_slope_center

COST = 2.0


def backtest_day_stop(closes, center, amp, absl, K, max_hold, stop_k, trend_thresh=None, mode="fade"):
    """mode: 'fade' (always bet reversion), 'skip' (skip entry if |slope|>trend_thresh),
    'flip' (bet WITH momentum instead of reversion if |slope|>trend_thresh)."""
    n = len(closes)
    trades = []
    in_pos = False
    for t in range(29, n):
        if np.isnan(amp[t]) or amp[t] <= 0 or np.isnan(center[t]):
            continue
        if not in_pos:
            z = (center[t] - closes[t]) / amp[t]
            if abs(z) >= K:
                trending = trend_thresh is not None and absl[t] >= trend_thresh
                if trending and mode == "skip":
                    continue
                fade_dir = 1 if z > 0 else -1
                direction = -fade_dir if (trending and mode == "flip") else fade_dir
                entry_price = closes[t]
                entry_center = center[t]
                entry_amp = amp[t]
                entry_idx = t
                in_pos = True
        else:
            adverse = direction * (entry_price - closes[t])  # positive = moving against us
            stopped = stop_k is not None and adverse >= stop_k * entry_amp
            touched = (direction == 1 and closes[t] >= entry_center) or (direction == -1 and closes[t] <= entry_center)
            timed_out = (t - entry_idx) >= max_hold
            end_of_day = t == n - 1
            if stopped or touched or timed_out or end_of_day:
                net = direction * (closes[t] - entry_price) - COST
                trades.append(dict(net=net, hold=t - entry_idx, exit_reason="stop" if stopped else ("touch" if touched else ("timeout" if timed_out else "eod"))))
                in_pos = False
    return trades


def summarize(all_trades_by_day, label):
    day_means = [np.mean([tr["net"] for tr in trs]) for trs in all_trades_by_day.values() if trs]
    if len(day_means) < 5:
        print(f"  {label:<40} (too few days)")
        return
    m, t, p = day_clustered(day_means)
    all_trades = [tr for trs in all_trades_by_day.values() for tr in trs]
    hit = np.mean([tr["net"] > 0 for tr in all_trades])
    reasons = {}
    for tr in all_trades:
        reasons[tr["exit_reason"]] = reasons.get(tr["exit_reason"], 0) + 1
    reason_str = " ".join(f"{k}={v/len(all_trades)*100:.0f}%" for k, v in sorted(reasons.items()))
    print(f"  {label:<40} n={len(all_trades):>5} hit={hit*100:>5.1f}%  net/trade(day-clust)={m:>8.2f}  t={t:>6.2f} p={p:>7.4f}  [{reason_str}]")


def main():
    bars_by_day = load_all_day_bars()
    series = {}
    for date, bars in bars_by_day.items():
        closes = bars["c"].to_numpy()
        _, center = rolling_slope_center(bars)
        series[date] = dict(closes=closes, center=center, amp=weighted_amp30(bars), absl=np.abs(slope30(bars)))

    K, max_hold = 1.5, 250
    print(f"=== stop-loss sweep, K={K} max_hold={max_hold} ===")
    for stop_k in (None, 0.5, 1.0, 1.5, 2.0, 3.0):
        by_day = {}
        for date, s in series.items():
            by_day[date] = backtest_day_stop(s["closes"], s["center"], s["amp"], s["absl"], K, max_hold, stop_k)
        summarize(by_day, f"stop_k={stop_k}")

    print(f"\n=== best stop_k combined with trend detection, K={K} max_hold={max_hold} ===")
    all_absl = np.concatenate([s["absl"][~np.isnan(s["absl"])] for s in series.values()])
    trend_thresh = np.percentile(all_absl, 75)
    print(f"  trend_thresh (75th pct of |slope|, pooled) = {trend_thresh:.2f}")
    for stop_k in (1.0, 1.5):
        for mode in ("fade", "skip", "flip"):
            by_day = {}
            for date, s in series.items():
                by_day[date] = backtest_day_stop(s["closes"], s["center"], s["amp"], s["absl"], K, max_hold, stop_k, trend_thresh, mode)
            summarize(by_day, f"stop_k={stop_k} mode={mode}")


if __name__ == "__main__":
    main()
