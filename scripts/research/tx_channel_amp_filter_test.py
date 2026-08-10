"""Ad-hoc round 3: does excluding/conditioning on strong-trend (|slope|) and
dirty-path (low efficiency ratio) regimes make the 30-min amplitude forecast
more accurate/stable on what's left?

Two things get reported per horizon/filter combo, both day-clustered (n=140
trading days, each day contributes one number, avoids the pooled-minute
overstated-significance trap from earlier rounds):
  - IC(amp, forward_amp) on the retained subsample (does correlation go up?)
  - std(surprise) and mean(|surprise|) on the retained subsample (does the
    forecast error actually get *tighter*, not just more correlated?)

Reuses load_all_day_bars / weighted_amp30 / forward_amp and the slope30 /
efficiency_ratio30 factor functions from the two prior rounds in this thread.
Not wired into any pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
from scipy import stats as sstats

from tx_channel_amp_volume_interaction import forward_amp, load_all_day_bars, weighted_amp30
from tx_channel_amp_persistence_drivers import slope30, day_clustered
from tx_channel_amp_persistence_drivers2 import efficiency_ratio30


def main():
    bars_by_day = load_all_day_bars()
    print(f"n days = {len(bars_by_day)}")

    series = {}
    for date, bars in bars_by_day.items():
        wamp = weighted_amp30(bars)
        series[date] = dict(
            bars=bars,
            wamp=wamp,
            abs_slope=np.abs(slope30(bars)),
            er=efficiency_ratio30(bars),
        )

    # pooled global percentile thresholds (judge "strong trend" / "dirty path" on an
    # absolute scale across the whole 140-day sample, not per-day-relative)
    all_slope = np.concatenate([s["abs_slope"][~np.isnan(s["abs_slope"])] for s in series.values()])
    all_er = np.concatenate([s["er"][~np.isnan(s["er"])] for s in series.values()])
    slope_p67 = np.percentile(all_slope, 67)
    slope_p90 = np.percentile(all_slope, 90)
    er_p33 = np.percentile(all_er, 33)
    er_p10 = np.percentile(all_er, 10)
    print(f"|slope| p67={slope_p67:.2f} p90={slope_p90:.2f} pts/min | ER p33={er_p33:.3f} p10={er_p10:.3f}")

    filters = {
        "全樣本（不過濾）": lambda sl, er: np.ones_like(sl, dtype=bool),
        "排除強趨勢 top1/3 |slope|": lambda sl, er: sl <= slope_p67,
        "排除強趨勢 top10% |slope|": lambda sl, er: sl <= slope_p90,
        "排除不乾淨 bottom1/3 ER": lambda sl, er: er >= er_p33,
        "排除不乾淨 bottom10% ER": lambda sl, er: er >= er_p10,
        "排除強趨勢top1/3 且 不乾淨bottom1/3": lambda sl, er: (sl <= slope_p67) & (er >= er_p33),
    }

    for h in (12, 35, 90):
        print(f"\n=== h={h} ===")
        for name, filt in filters.items():
            ics, stds, mads, n_ratio = [], [], [], []
            for date, s in series.items():
                wamp, sl, er = s["wamp"], s["abs_slope"], s["er"]
                famp = forward_amp(s["bars"], h)
                valid = ~(np.isnan(wamp) | np.isnan(sl) | np.isnan(er) | np.isnan(famp))
                if valid.sum() < 20:
                    continue
                a, slv, erv, y = wamp[valid], sl[valid], er[valid], famp[valid]
                mask = filt(slv, erv)
                if mask.sum() < 20:
                    continue
                a2, y2 = a[mask], y[mask]
                surprise = y2 - a2
                ic, _ = sstats.spearmanr(a2, y2)
                ics.append(ic)
                stds.append(surprise.std())
                mads.append(np.abs(surprise).mean())
                n_ratio.append(mask.mean())
            icm, ict, icp = day_clustered(ics)
            stdm, _, _ = day_clustered(stds)
            madm, _, _ = day_clustered(mads)
            nr = np.mean(n_ratio) if n_ratio else float("nan")
            print(f"  {name:<38} 保留{nr*100:>5.1f}% | IC={icm:>7.4f}(t={ict:>6.2f},p={icp:>7.4f}) | std(surprise)={stdm:>9.1f} | MAE(surprise)={madm:>9.1f}")


if __name__ == "__main__":
    main()
