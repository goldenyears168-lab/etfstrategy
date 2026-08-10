"""Ad-hoc round 11: concrete trading-signal design requested by the user —
"how far from the mean (in amplitude units) should trigger a long/short bet,
held until price reverts to the mean" — not a fixed-horizon exit like round
6-8 tested (which found no economic edge), but an adaptive "hold until touch"
exit, which round 6-8 never tried.

Signal: z_dev[t] = (center[t]-close[t]) / amp[t]   (30-min regression center
minus price, normalized by 30-min weighted amplitude — the "stretch ratio"
factor from round 7, which showed monotonically stronger reversion IC at
bigger stretch, with no breakout reversal even at the extremes).

Rule: when |z_dev[t]| >= K, enter in the reversion direction (price below
center by >= K amplitude-units -> long; above -> short). Exit on the first
bar where price touches the entry-time center, or at a max-holding-time cap
(prevents unbounded risk if reversion never comes), or forced flat at session
close (13:45) — whichever comes first. One position at a time per day.

Sweeps K and the holding-time cap; day-clustered significance throughout
(140 trading days, corrected ticks_to_1m_bars — the contract-month/spread-
quote bug fixed earlier this thread). Not wired into any pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
from scipy import stats as sstats

from tx_channel_amp_volume_interaction import load_all_day_bars, weighted_amp30
from tx_channel_amp_persistence_drivers import day_clustered
from tx_channel_slope_persistence_140d import rolling_slope_center

COST = 2.0


def backtest_day(closes, center, amp, K, max_hold):
    """Returns list of (net_pts, hold_minutes, direction) trades for one day."""
    n = len(closes)
    trades = []
    in_pos = False
    for t in range(29, n):
        if amp[t] is None or np.isnan(amp[t]) or amp[t] <= 0 or np.isnan(center[t]):
            continue
        if not in_pos:
            z = (center[t] - closes[t]) / amp[t]
            if abs(z) >= K:
                direction = 1 if z > 0 else -1  # price below center -> long; above -> short
                entry_price = closes[t]
                entry_center = center[t]
                entry_idx = t
                in_pos = True
        else:
            touched = (direction == 1 and closes[t] >= entry_center) or (direction == -1 and closes[t] <= entry_center)
            timed_out = (t - entry_idx) >= max_hold
            end_of_day = t == n - 1
            if touched or timed_out or end_of_day:
                exit_price = closes[t]
                net = direction * (exit_price - entry_price) - COST
                trades.append((net, t - entry_idx, direction, touched))
                in_pos = False
    return trades


def main():
    bars_by_day = load_all_day_bars()
    dates = sorted(bars_by_day)
    print(f"n days = {len(dates)}")

    series = {}
    for date, bars in bars_by_day.items():
        closes = bars["c"].to_numpy()
        _, center = rolling_slope_center(bars)
        amp = weighted_amp30(bars)
        series[date] = dict(closes=closes, center=center, amp=amp)

    print(f"\n{'K':>5} {'max_hold':>9} {'n_trades':>9} {'trades/day':>11} {'avg_hold':>9} {'touch%':>8} {'hit%':>7} {'net_pts/trade':>14} {'t':>7} {'p':>8}")
    for K in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
        for max_hold in (60, 120, 250):
            day_means = []
            all_holds, all_touch, all_hit, n_total = [], [], [], 0
            for date, s in series.items():
                trades = backtest_day(s["closes"], s["center"], s["amp"], K, max_hold)
                if not trades:
                    continue
                nets = np.array([tr[0] for tr in trades])
                day_means.append(nets.mean())
                all_holds.extend(tr[1] for tr in trades)
                all_touch.extend(tr[3] for tr in trades)
                all_hit.extend(tr[0] > 0 for tr in trades)
                n_total += len(trades)
            if not day_means:
                continue
            mm, t, p = day_clustered(day_means)
            n_days_with_trades = len(day_means)
            print(f"{K:>5.1f} {max_hold:>9} {n_total:>9} {n_total/len(dates):>11.2f} {np.mean(all_holds):>9.1f} {np.mean(all_touch)*100:>7.1f}% {np.mean(all_hit)*100:>6.1f}% {mm:>14.3f} {t:>7.2f} {p:>8.4f}  (days_with_trades={n_days_with_trades})")


if __name__ == "__main__":
    main()
