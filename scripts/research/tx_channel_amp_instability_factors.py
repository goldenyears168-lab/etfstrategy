"""Ad-hoc round 9: not "which way will amplitude move" but "is a big forecast
miss coming, regardless of direction" — test candidate causal factors against
|surprise| (absolute forecast error of the naive amp-persistence forecast),
not signed surprise. A factor with real IC here flags forecast instability /
imminent regime change even without telling you which way it breaks.

Uses the corrected ticks_to_1m_bars (contract-month/spread-quote bug fixed
earlier this thread). All factors are causal (t uses only <=t information).
Not wired into any pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
from scipy import stats as sstats

from tx_channel_amp_volume_interaction import WINDOW, forward_amp, load_all_day_bars, weighted_amp30
from tx_channel_amp_persistence_drivers import day_clustered, slope30
from tx_channel_amp_persistence_drivers2 import efficiency_ratio30, r2_30, vol_level30, z_extreme


def vol_trend30(bars):
    vol = bars["v"].to_numpy()
    n = len(bars)
    x = np.arange(WINDOW)
    out = np.full(n, np.nan)
    for i in range(WINDOW - 1, n):
        seg = vol[i - WINDOW + 1 : i + 1]
        m = seg.mean()
        if m <= 0:
            continue
        s, _ = np.polyfit(x, seg, 1)
        out[i] = s / m
    return out


def main():
    bars_by_day = load_all_day_bars()
    print(f"n days = {len(bars_by_day)}")

    series = {}
    for date, bars in bars_by_day.items():
        wamp = weighted_amp30(bars)
        series[date] = dict(
            bars=bars,
            wamp=wamp,
            z=z_extreme(wamp),
            abs_z=np.abs(z_extreme(wamp)),
            vlvl=vol_level30(bars),
            abs_vtrend=np.abs(vol_trend30(bars)),
            er=efficiency_ratio30(bars),
            r2=r2_30(bars),
            abs_slope=np.abs(slope30(bars)),
        )

    factors = ["abs_z", "vlvl", "abs_vtrend", "er", "r2", "abs_slope", "wamp"]
    labels = {
        "abs_z": "|z_extreme|（偏離今日基準的絕對程度）",
        "vlvl": "成交量水準",
        "abs_vtrend": "|成交量趨勢|（量能變化幅度,不分方向）",
        "er": "efficiency_ratio(路徑效率)",
        "r2": "R2(貼合回歸線)",
        "abs_slope": "|回歸線斜率|(趨勢強度)",
        "wamp": "目前振幅水準本身",
    }

    for h in (12, 35, 90):
        print(f"\n=== h={h}: IC(factor, |surprise|) — 預測「誤差會不會變大」，不管方向 ===")
        for f in factors:
            ics = []
            for date, s in series.items():
                wamp, fac = s["wamp"], s[f]
                famp = forward_amp(s["bars"], h)
                valid = ~(np.isnan(wamp) | np.isnan(fac) | np.isnan(famp))
                if valid.sum() < 20:
                    continue
                a, x, y = wamp[valid], fac[valid], famp[valid]
                abs_surprise = np.abs(y - a)
                ic, _ = sstats.spearmanr(x, abs_surprise)
                ics.append(ic)
            m, t, p = day_clustered(ics)
            print(f"  {labels[f]:<34} IC={m:>8.4f}  t={t:>6.2f}  p={p:>7.4f}")


if __name__ == "__main__":
    main()
