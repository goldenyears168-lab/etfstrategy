"""Ad-hoc round 6: redo the regression-line (slope/trend-continuation and
center/reversion) predictive-decay test from earlier in this thread, but
with the full 140-day tick-cache sample and day-clustered significance
instead of the original 5-day pooled-HAC pass. Same horizon sweep used for
the amplitude decay curve (1..180min) so the two are directly comparable.

Two hypotheses, both causal (channel at t uses only t-29..t):
  trend      bet direction = sign(slope[t])          forward price return
  reversion  bet direction = sign(center[t] - c[t])   forward price return

Reuses load_all_day_bars from tx_channel_amp_volume_interaction.py.
Not wired into any pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
from scipy import stats as sstats

from tx_channel_amp_volume_interaction import WINDOW, load_all_day_bars
from tx_channel_amp_persistence_drivers import day_clustered

HORIZONS = [1, 2, 3, 5, 8, 12, 18, 25, 35, 45, 60, 75, 90, 120, 150, 180]


def rolling_slope_center(bars) -> tuple:
    closes = bars["c"].to_numpy()
    n = len(bars)
    x = np.arange(WINDOW)
    slope = np.full(n, np.nan)
    center = np.full(n, np.nan)
    for i in range(WINDOW - 1, n):
        y = closes[i - WINDOW + 1 : i + 1]
        s, b = np.polyfit(x, y, 1)
        slope[i] = s
        center[i] = s * (WINDOW - 1) + b
    return slope, center


def main():
    bars_by_day = load_all_day_bars()
    dates = sorted(bars_by_day)
    print(f"n days = {len(dates)}")

    series = {}
    for date, bars in bars_by_day.items():
        closes = bars["c"].to_numpy()
        slope, center = rolling_slope_center(bars)
        series[date] = dict(closes=closes, slope=slope, center=center, n=len(bars))

    print(f"\n{'h':>4} {'trend_IC':>9} {'trend_t':>8} {'trend_p':>9} {'trend_hit':>10} | {'rev_IC':>9} {'rev_t':>8} {'rev_p':>9} {'rev_hit':>9}")
    for h in HORIZONS:
        trend_ics, trend_hits, rev_ics, rev_hits = [], [], [], []
        for date, s in series.items():
            closes, slope, center, n = s["closes"], s["slope"], s["center"], s["n"]
            valid_idx = np.where(~np.isnan(slope))[0]
            valid_idx = valid_idx[valid_idx + h < n]
            if len(valid_idx) < 20:
                continue
            c0 = closes[valid_idx]
            fwd = closes[valid_idx + h] - c0
            sl = slope[valid_idx]
            ce = center[valid_idx]

            ic_t, _ = sstats.spearmanr(sl, fwd)
            trend_ics.append(ic_t)
            td = np.sign(sl)
            m = td != 0
            trend_hits.append((np.sign(fwd[m]) == td[m]).mean())

            dev = ce - c0
            ic_r, _ = sstats.spearmanr(dev, fwd)
            rev_ics.append(ic_r)
            rd = np.sign(dev)
            m = rd != 0
            rev_hits.append((np.sign(fwd[m]) == rd[m]).mean())

        tm, tt, tp = day_clustered(trend_ics)
        rm, rt, rp = day_clustered(rev_ics)
        th = np.mean(trend_hits)
        rh = np.mean(rev_hits)
        print(f"{h:>4} {tm:>9.4f} {tt:>8.2f} {tp:>9.4f} {th*100:>9.1f}% | {rm:>9.4f} {rt:>8.2f} {rp:>9.4f} {rh*100:>8.1f}%")


if __name__ == "__main__":
    main()
