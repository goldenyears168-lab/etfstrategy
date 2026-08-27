"""Ad-hoc round 8: is the "trade reversion only when |slope| is low (range-bound),
skip when |slope| is high (trending)" filter actually usable in real time?

Round 7's tercile split used a GLOBAL percentile threshold computed from all
140 days pooled — that peeks at future days to classify an earlier day's
regime, which a live system could never do. This redoes it walk-forward: the
"is |slope| currently low?" threshold is a trailing rolling percentile
computed only from prior days (same discipline as round 5's rolling amp
calibration). Then re-checks whether the net-of-cost economics from round 7
(essentially zero even in the best subgroup) survive under this properly
causal filter, or were partly an artifact of the look-ahead threshold.

Uses the corrected ticks_to_1m_bars (contract-month/spread-quote bug fixed
earlier this thread). Not wired into any pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
from scipy import stats as sstats

from tx_channel_amp_volume_interaction import load_all_day_bars
from tx_channel_amp_persistence_drivers import slope30, day_clustered
from tx_channel_slope_persistence_140d import rolling_slope_center

WARMUP_DAYS = 30
ROLL_WINDOW = 20  # trading days of trailing history used to set "what counts as low |slope| right now"
COST = 2.0


def main():
    bars_by_day = load_all_day_bars()
    dates = sorted(bars_by_day)
    print(f"n days = {len(dates)}")

    series = {}
    for date, bars in bars_by_day.items():
        closes = bars["c"].to_numpy()
        slope, center = rolling_slope_center(bars)
        series[date] = dict(closes=closes, center=center, abs_slope=np.abs(slope30(bars)), n=len(bars))

    for h in (35, 90, 120):
        print(f"\n=== h={h} ===")
        naive_day, filt_day, unfilt_day = [], [], []
        naive_hit, filt_hit = [], []
        n_traded_ratio = []
        thresholds_used = []
        for i, date in enumerate(dates):
            if i < WARMUP_DAYS:
                continue
            s = series[date]
            closes, center, absl, n = s["closes"], s["center"], s["abs_slope"], s["n"]
            valid_idx = np.where(~np.isnan(center) & ~np.isnan(absl))[0]
            valid_idx = valid_idx[valid_idx + h < n]
            if len(valid_idx) < 15:
                continue

            # causal threshold: 33rd percentile of |slope| pooled over the trailing
            # ROLL_WINDOW trading days (never includes today or the future)
            prior_dates = dates[max(0, i - ROLL_WINDOW) : i]
            prior_slopes = np.concatenate(
                [series[p]["abs_slope"][~np.isnan(series[p]["abs_slope"])] for p in prior_dates]
            )
            if len(prior_slopes) < 500:
                continue
            q33 = np.percentile(prior_slopes, 33)
            thresholds_used.append(q33)

            c0 = closes[valid_idx]
            fwd = closes[valid_idx + h] - c0
            dev = center[valid_idx] - c0
            rd = np.sign(dev)
            m_all = rd != 0
            net_all = rd[m_all] * fwd[m_all] - COST

            low_now = absl[valid_idx] <= q33
            m_filt = m_all & low_now
            if m_filt.sum() < 5:
                continue
            net_filt = rd[m_filt] * fwd[m_filt] - COST

            naive_day.append(net_all.mean())
            filt_day.append(net_filt.mean())
            naive_hit.append((np.sign(fwd[m_all]) == rd[m_all]).mean())
            filt_hit.append((np.sign(fwd[m_filt]) == rd[m_filt]).mean())
            n_traded_ratio.append(m_filt.sum() / m_all.sum())

        nm, nt, np_ = day_clustered(naive_day)
        fm, ft, fp = day_clustered(filt_day)
        print(f"  causal q33 threshold used: mean={np.mean(thresholds_used):.2f} (recomputed every day from trailing {ROLL_WINDOW}d)")
        print(f"  n scored days = {len(naive_day)}, avg %% of minutes traded under filter = {np.mean(n_traded_ratio)*100:.1f}%%")
        print(f"  {'':<10} {'net_pts/trade':>14} {'t':>7} {'p':>8} {'hit%':>7}")
        print(f"  {'naive(全交易)':<10} {nm:>14.3f} {nt:>7.2f} {np_:>8.4f} {np.mean(naive_hit)*100:>6.1f}%")
        print(f"  {'即時過濾':<10} {fm:>14.3f} {ft:>7.2f} {fp:>8.4f} {np.mean(filt_hit)*100:>6.1f}%")

        d, t, p = day_clustered(np.array(filt_day) - np.array(naive_day))
        print(f"  paired (day-clustered) 過濾 vs 不過濾: Δ={d:.3f}  t={t:.2f}  p={p:.4f}")


if __name__ == "__main__":
    main()
