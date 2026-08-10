"""Ad-hoc round 2: hunt for what actually explains amplitude's failure to
persist past ~90min (ruled out in round 1: time-of-day, price direction;
partially explained: |slope| trend-strength, ~1/5 of the effect).

New candidate factors, all causal (no look-ahead within a day):
  z_extreme   — how far today's current 30-min amp sits above/below its own
                running (expanding, same-day) mean, in std units. Tests the
                classic "extremes revert" mechanism directly.
  ER (efficiency ratio) — Kaufman's efficiency ratio of the 30-min price path:
                |net displacement| / sum(|bar-to-bar moves|). 1 = clean
                one-directional move, ~0 = pure chop/whipsaw. Different from
                |slope| (which only sees net displacement, not path quality).
  R2          — R^2 of the price-vs-time OLS fit over the same 30-min window
                (how well price actually tracks its own regression line).
  vol_level   — plain trailing 30-min average volume (not the trend tested
                before) as a liquidity/participation proxy.

Reuses load_all_day_bars / weighted_amp30 / forward_amp from
tx_channel_amp_volume_interaction.py and slope30 from
tx_channel_amp_persistence_drivers.py. Not wired into any pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
import pandas as pd
from scipy import stats as sstats

from tx_channel_amp_volume_interaction import WINDOW, forward_amp, load_all_day_bars, weighted_amp30
from tx_channel_amp_persistence_drivers import slope30, day_clustered


def efficiency_ratio30(bars: pd.DataFrame) -> np.ndarray:
    closes = bars["c"].to_numpy()
    n = len(bars)
    out = np.full(n, np.nan)
    for i in range(WINDOW - 1, n):
        seg = closes[i - WINDOW + 1 : i + 1]
        disp = abs(seg[-1] - seg[0])
        dist = np.abs(np.diff(seg)).sum()
        out[i] = disp / dist if dist > 0 else np.nan
    return out


def r2_30(bars: pd.DataFrame) -> np.ndarray:
    closes = bars["c"].to_numpy()
    n = len(bars)
    x = np.arange(WINDOW)
    out = np.full(n, np.nan)
    for i in range(WINDOW - 1, n):
        y = closes[i - WINDOW + 1 : i + 1]
        b = np.polyfit(x, y, 1)
        resid = y - np.polyval(b, x)
        tot = y.var()
        out[i] = 1 - resid.var() / tot if tot > 0 else np.nan
    return out


def vol_level30(bars: pd.DataFrame) -> np.ndarray:
    vol = bars["v"].to_numpy()
    n = len(bars)
    out = np.full(n, np.nan)
    for i in range(WINDOW - 1, n):
        out[i] = vol[i - WINDOW + 1 : i + 1].mean()
    return out


def z_extreme(wamp: np.ndarray, min_hist: int = 10) -> np.ndarray:
    """Same-day expanding z-score of wamp: how extreme is "now" vs today's
    own running average so far (fully causal — only past-within-day data)."""
    n = len(wamp)
    out = np.full(n, np.nan)
    valid_idx = np.where(~np.isnan(wamp))[0]
    if len(valid_idx) == 0:
        return out
    hist = []
    for i in range(n):
        if not np.isnan(wamp[i]):
            if len(hist) >= min_hist:
                m = np.mean(hist)
                s = np.std(hist)
                if s > 0:
                    out[i] = (wamp[i] - m) / s
            hist.append(wamp[i])
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
            slope=slope30(bars),
            er=efficiency_ratio30(bars),
            r2=r2_30(bars),
            vlvl=vol_level30(bars),
            z=z_extreme(wamp),
        )

    factors = ["z", "er", "r2", "vlvl"]
    labels = {"z": "z_extreme(當下vs今日均值)", "er": "efficiency_ratio(路徑效率)", "r2": "R2(貼合回歸線程度)", "vlvl": "volume_level(成交量水準)"}

    print("\n=== contemporaneous corr(amp, factor) ===")
    for f in factors:
        cs = []
        for s in series.values():
            valid = ~(np.isnan(s["wamp"]) | np.isnan(s[f]))
            if valid.sum() < 20:
                continue
            c, _ = sstats.spearmanr(s["wamp"][valid], s[f][valid])
            cs.append(c)
        m, t, p = day_clustered(cs)
        print(f"  {labels[f]:<28} corr={m:.3f} t={t:.2f} p={p:.4f}")

    print("\n=== IC(factor, forward_amp) and IC(factor, surprise=fwd_amp-amp) by horizon ===")
    for h in (12, 35, 90):
        print(f"--- h={h} ---")
        for f in factors:
            ic_fwd, ic_sur = [], []
            for s in series.values():
                wamp, fac, n = s["wamp"], s[f], len(s["wamp"])
                famp = forward_amp(s["bars"], h)
                valid = ~(np.isnan(wamp) | np.isnan(fac) | np.isnan(famp))
                if valid.sum() < 20:
                    continue
                a, x, y = wamp[valid], fac[valid], famp[valid]
                surprise = y - a
                icf, _ = sstats.spearmanr(x, y)
                ics, _ = sstats.spearmanr(x, surprise)
                ic_fwd.append(icf)
                ic_sur.append(ics)
            mf, tf, pf = day_clustered(ic_fwd)
            ms, ts, ps = day_clustered(ic_sur)
            print(f"  {labels[f]:<28} IC(x,fwd_amp)={mf:>7.4f} t={tf:>6.2f} p={pf:>7.4f}  |  IC(x,surprise)={ms:>7.4f} t={ts:>6.2f} p={ps:>7.4f}")

    print("\n=== does amp's own predictive power survive after controlling for z_extreme (the strongest candidate)? ===")
    for h in (12, 35, 90):
        ic_resid = []
        for s in series.values():
            wamp, z, n = s["wamp"], s["z"], len(s["wamp"])
            famp = forward_amp(s["bars"], h)
            valid = ~(np.isnan(wamp) | np.isnan(z) | np.isnan(famp))
            if valid.sum() < 20:
                continue
            a, zz, y = wamp[valid], z[valid], famp[valid]
            b = np.polyfit(zz, a, 1)
            resid = a - np.polyval(b, zz)
            icr, _ = sstats.spearmanr(resid, y)
            ic_resid.append(icr)
        m, t, p = day_clustered(ic_resid)
        print(f"  h={h}: amp-residual(on z)->fwd_amp IC={m:.4f} t={t:.2f} p={p:.4f}")

    print("\n=== z_extreme tercile breakdown: mean surprise by how extreme amp currently is (h=90) ===")
    h = 90
    lo, mid, hi = [], [], []
    for s in series.values():
        wamp, z = s["wamp"], s["z"]
        famp = forward_amp(s["bars"], h)
        valid = ~(np.isnan(wamp) | np.isnan(z) | np.isnan(famp))
        if valid.sum() < 20:
            continue
        a, zz, y = wamp[valid], z[valid], famp[valid]
        surprise = y - a
        q1, q2 = np.percentile(zz, [33, 67])
        lo.append(surprise[zz <= q1].mean())
        mid.append(surprise[(zz > q1) & (zz <= q2)].mean())
        hi.append(surprise[zz > q2].mean())
    for name, arr in (("z 低 (振幅偏低)", lo), ("z 中", mid), ("z 高 (振幅偏高/極端)", hi)):
        m, t, p = day_clustered(arr)
        print(f"  {name}: mean_surprise={m:>8.2f}  t={t:.2f} p={p:.4f}")


if __name__ == "__main__":
    main()
