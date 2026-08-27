"""Ad-hoc round 7: what factors modulate the regression-line reversion signal
(dev = center[t]-c[t] predicting forward price return)? Trend-following
direction was already ruled out entirely (round 6: negative at every horizon,
0 continuation value). This asks the follow-up: for the one signal that IS
statistically real (reversion, significant from ~12-18min onward, growing to
h=180), what makes it stronger or weaker?

Candidate factors, mirroring the amplitude-driver hunt (round 2):
  ER          path efficiency (clean trend vs choppy whipsaw)
  abs_slope   trend strength
  amp_level   current 30-min amplitude (volatility regime)
  dev_z       how extreme the current price deviation from center is,
              normalized by the window's own amplitude (relative stretch)
  time-of-day session minute bucket

Uses the CORRECTED ticks_to_1m_bars (contract-month + spread-quote bug fixed
earlier this thread). Reuses load_all_day_bars, weighted_amp30,
efficiency_ratio30, slope30, day_clustered from prior rounds. Not wired into
any pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
from scipy import stats as sstats

from tx_channel_amp_volume_interaction import WINDOW, load_all_day_bars, weighted_amp30
from tx_channel_amp_persistence_drivers import slope30, day_clustered
from tx_channel_amp_persistence_drivers2 import efficiency_ratio30
from tx_channel_slope_persistence_140d import rolling_slope_center

HORIZONS = [35, 90, 120]


def main():
    bars_by_day = load_all_day_bars()
    print(f"n days = {len(bars_by_day)}")

    series = {}
    for date, bars in bars_by_day.items():
        closes = bars["c"].to_numpy()
        slope, center = rolling_slope_center(bars)
        series[date] = dict(
            closes=closes,
            slope=slope,
            center=center,
            abs_slope=np.abs(slope30(bars)),
            er=efficiency_ratio30(bars),
            amp=weighted_amp30(bars),
            n=len(bars),
        )

    # dev_z: how extreme is the current price relative to the channel, scaled by
    # the window's own amplitude (a "stretch ratio" of the channel)
    for s in series.values():
        dev = s["center"] - s["closes"]
        s["dev"] = dev
        with np.errstate(divide="ignore", invalid="ignore"):
            s["dev_z"] = np.where(s["amp"] > 0, np.abs(dev) / s["amp"], np.nan)

    factors = ["er", "abs_slope", "amp", "dev_z"]
    labels = {"er": "efficiency_ratio(路徑效率)", "abs_slope": "|slope|(趨勢強度)", "amp": "30分振幅水準", "dev_z": "偏離幅度/振幅(伸展比)"}

    # pooled global tercile thresholds
    pooled = {f: np.concatenate([s[f][~np.isnan(s[f])] for s in series.values()]) for f in factors}
    thresh = {f: np.percentile(pooled[f], [33, 67]) for f in factors}

    for h in HORIZONS:
        print(f"\n=== h={h}: reversion IC(dev, fwd_return) split by factor tercile ===")
        for f in factors:
            lo, mid, hi = [], [], []
            for date, s in series.items():
                closes, center, dev, n, fac = s["closes"], s["center"], s["dev"], s["n"], s[f]
                valid_idx = np.where(~np.isnan(center) & ~np.isnan(fac))[0]
                valid_idx = valid_idx[valid_idx + h < n]
                if len(valid_idx) < 15:
                    continue
                c0 = closes[valid_idx]
                fwd = closes[valid_idx + h] - c0
                d = dev[valid_idx]
                fv = fac[valid_idx]
                q1, q2 = thresh[f]
                for bucket, store in ((fv <= q1, lo), ((fv > q1) & (fv <= q2), mid), (fv > q2, hi)):
                    if bucket.sum() < 15:
                        continue
                    ic, _ = sstats.spearmanr(d[bucket], fwd[bucket])
                    store.append(ic)
            lm, lt, lp = day_clustered(lo)
            mm, mt, mp = day_clustered(mid)
            hm, ht, hp = day_clustered(hi)
            print(f"  {labels[f]:<26} 低: IC={lm:>7.4f}(t={lt:>5.2f},p={lp:>6.4f})  中: IC={mm:>7.4f}(t={mt:>5.2f},p={mp:>6.4f})  高: IC={hm:>7.4f}(t={ht:>5.2f},p={hp:>6.4f})")

    # --- time-of-day split ---
    print("\n=== time-of-day (minute-of-session tercile): reversion IC ===")
    for h in HORIZONS:
        early, midday, late = [], [], []
        for date, s in series.items():
            closes, center, dev, n = s["closes"], s["center"], s["dev"], s["n"]
            valid_idx = np.where(~np.isnan(center))[0]
            valid_idx = valid_idx[valid_idx + h < n]
            if len(valid_idx) < 15:
                continue
            c0 = closes[valid_idx]
            fwd = closes[valid_idx + h] - c0
            d = dev[valid_idx]
            mos = valid_idx  # minute-of-session index, 0..n-1
            for bucket, store in ((mos < 100, early), ((mos >= 100) & (mos < 200), midday), (mos >= 200, late)):
                if bucket.sum() < 15:
                    continue
                ic, _ = sstats.spearmanr(d[bucket], fwd[bucket])
                store.append(ic)
        em, et, ep = day_clustered(early)
        mm, mt, mp = day_clustered(midday)
        lm, lt, lp = day_clustered(late)
        print(f"  h={h:>4}  早盤(08:45-10:25): IC={em:>7.4f}(t={et:>5.2f},p={ep:>6.4f})  午前(10:25-12:05): IC={mm:>7.4f}(t={mt:>5.2f},p={mp:>6.4f})  午後(12:05-13:45): IC={lm:>7.4f}(t={lt:>5.2f},p={lp:>6.4f})")


if __name__ == "__main__":
    main()
