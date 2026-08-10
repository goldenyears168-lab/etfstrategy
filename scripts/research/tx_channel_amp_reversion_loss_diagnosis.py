"""Ad-hoc round 12: diagnose what distinguishes the losing trades (price never
reverts to the entry-time center, forced out at max-hold/EOD) from the
winning trades (price does revert) in the round-11 "hold until reversion"
backtest. Records entry-time causal factors for every trade and compares.

Candidate factors, all already built earlier this thread:
  abs_slope   trend strength at entry (round 7's dominant conditioner)
  er          path efficiency at entry
  minute-of-session  time of day at entry
  z_stretch   how many amplitude-units price was from center at entry (signal strength itself)
  vlvl        volume level at entry

Uses the corrected ticks_to_1m_bars. Not wired into any pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
from scipy import stats as sstats

from tx_channel_amp_volume_interaction import load_all_day_bars, weighted_amp30
from tx_channel_amp_persistence_drivers import day_clustered, slope30
from tx_channel_amp_persistence_drivers2 import efficiency_ratio30, vol_level30
from tx_channel_slope_persistence_140d import rolling_slope_center

COST = 2.0


def backtest_day_with_factors(closes, center, amp, absl, er, vlvl, K, max_hold):
    n = len(closes)
    trades = []
    in_pos = False
    for t in range(29, n):
        if amp[t] is None or np.isnan(amp[t]) or amp[t] <= 0 or np.isnan(center[t]):
            continue
        if not in_pos:
            z = (center[t] - closes[t]) / amp[t]
            if abs(z) >= K:
                direction = 1 if z > 0 else -1
                entry_price = closes[t]
                entry_center = center[t]
                entry_idx = t
                entry_factors = dict(abs_slope=absl[t], er=er[t], mos=t, vlvl=vlvl[t], z_stretch=abs(z))
                in_pos = True
        else:
            touched = (direction == 1 and closes[t] >= entry_center) or (direction == -1 and closes[t] <= entry_center)
            timed_out = (t - entry_idx) >= max_hold
            end_of_day = t == n - 1
            if touched or timed_out or end_of_day:
                exit_price = closes[t]
                net = direction * (exit_price - entry_price) - COST
                trades.append(dict(net=net, hold=t - entry_idx, touched=touched, **entry_factors))
                in_pos = False
    return trades


def main():
    bars_by_day = load_all_day_bars()
    dates = sorted(bars_by_day)

    series = {}
    for date, bars in bars_by_day.items():
        closes = bars["c"].to_numpy()
        _, center = rolling_slope_center(bars)
        series[date] = dict(
            closes=closes,
            center=center,
            amp=weighted_amp30(bars),
            absl=slope30(bars),
            absl_abs=np.abs(slope30(bars)),
            er=efficiency_ratio30(bars),
            vlvl=vol_level30(bars),
        )

    for K, max_hold in ((1.5, 250), (2.0, 250)):
        print(f"\n{'='*70}\nK={K} max_hold={max_hold}\n{'='*70}")
        all_trades = []
        for date, s in series.items():
            trades = backtest_day_with_factors(s["closes"], s["center"], s["amp"], s["absl_abs"], s["er"], s["vlvl"], K, max_hold)
            for tr in trades:
                tr["date"] = date
            all_trades.extend(trades)

        touched = [tr for tr in all_trades if tr["touched"]]
        not_touched = [tr for tr in all_trades if not tr["touched"]]
        print(f"n_trades={len(all_trades)}  touched={len(touched)} (mean_net={np.mean([t['net'] for t in touched]):.1f})  "
              f"not_touched={len(not_touched)} (mean_net={np.mean([t['net'] for t in not_touched]):.1f})")

        print(f"\n{'factor':<14} {'touched_mean':>13} {'not_touched_mean':>17} {'diff':>10} {'t':>7} {'p':>8}")
        for f in ("abs_slope", "er", "mos", "vlvl", "z_stretch"):
            a = np.array([tr[f] for tr in touched])
            b = np.array([tr[f] for tr in not_touched])
            t, p = sstats.ttest_ind(a, b, equal_var=False)
            print(f"  {f:<12} {a.mean():>13.3f} {b.mean():>17.3f} {a.mean()-b.mean():>10.3f} {t:>7.2f} {p:>8.4f}")

        # day-clustered version: does per-day mean(abs_slope at entry) predict per-day trade outcome?
        print("\n  day-clustered: IC(entry factor, trade net_pts) pooled across all trades, by day")
        for f in ("abs_slope", "er", "mos", "vlvl", "z_stretch"):
            per_day_ic = []
            by_day = {}
            for tr in all_trades:
                by_day.setdefault(tr["date"], []).append(tr)
            for date, trs in by_day.items():
                if len(trs) < 5:
                    continue
                x = np.array([tr[f] for tr in trs])
                y = np.array([tr["net"] for tr in trs])
                if np.std(x) == 0:
                    continue
                ic, _ = sstats.spearmanr(x, y)
                if not np.isnan(ic):
                    per_day_ic.append(ic)
            m, t, p = day_clustered(per_day_ic)
            print(f"    {f:<12} IC={m:>8.4f}  t={t:>6.2f}  p={p:>7.4f}  (n_days={len(per_day_ic)})")


if __name__ == "__main__":
    main()
