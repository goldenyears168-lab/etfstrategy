"""Ad-hoc: why does 30-min amplitude persistence decay/reverse at ~90-180min?
Three candidate drivers tested against 140 days of real day-session tick data:

1. Time-of-day (session shape) — TX day session has a structural
   volatility curve (open burst -> midday lull -> close pickup). If forward
   amplitude just tracks that deterministic curve, "persistence" at short
   horizons and "reversal" at 90-180min could be a session-shape artifact,
   not real autoregressive clustering. Tested by de-meaning amp against the
   pooled session-shape curve and re-running the IC.
2. Trend strength (|regression slope|) — does a fast-moving market (big
   |slope|) explain amplitude better than amplitude explains itself?
3. Price direction (sign of slope) — does persistence differ between
   up-trend and down-trend regimes?

Reuses ticks_to_1m_bars / weighted_amp30 / load_all_day_bars from
tx_channel_amp_volume_interaction.py. Not wired into any pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
import pandas as pd
from scipy import stats as sstats

from tx_channel_amp_volume_interaction import (
    HORIZONS,
    WINDOW,
    forward_amp,
    load_all_day_bars,
    weighted_amp30,
)


def slope30(bars: pd.DataFrame) -> np.ndarray:
    closes = bars["c"].to_numpy()
    n = len(bars)
    x = np.arange(WINDOW)
    out = np.full(n, np.nan)
    for i in range(WINDOW - 1, n):
        y = closes[i - WINDOW + 1 : i + 1]
        s, _ = np.polyfit(x, y, 1)
        out[i] = s
    return out


def day_clustered(vals):
    vals = np.array(vals)
    vals = vals[~np.isnan(vals)]
    if len(vals) < 3:
        return np.nan, np.nan, np.nan
    t, p = sstats.ttest_1samp(vals, 0.0)
    return float(vals.mean()), float(t), float(p)


def main():
    bars_by_day = load_all_day_bars()
    print(f"n days = {len(bars_by_day)}")

    # precompute per-day series
    series = {}
    for date, bars in bars_by_day.items():
        wamp = weighted_amp30(bars)
        slope = slope30(bars)
        series[date] = dict(bars=bars, wamp=wamp, slope=slope, n=len(bars))

    # === Test 1: session-shape (time-of-day) curve ===
    print("\n=== Test 1: session-shape curve (mean weighted-amp by minute-of-session, pooled 140 days) ===")
    max_n = max(s["n"] for s in series.values())
    shape_sum = np.zeros(max_n)
    shape_cnt = np.zeros(max_n)
    for s in series.values():
        w = s["wamp"]
        valid = ~np.isnan(w)
        shape_sum[: len(w)][valid] += w[valid]
        shape_cnt[: len(w)][valid] += 1
    shape = np.divide(shape_sum, shape_cnt, out=np.full(max_n, np.nan), where=shape_cnt > 0)
    bucket = 15
    for start in range(0, max_n, bucket):
        seg = shape[start : start + bucket]
        seg = seg[~np.isnan(seg)]
        if len(seg):
            hh = 8 * 60 + 45 + start
            print(f"  min-of-session {start:>3}-{start+bucket-1:<3} (~{hh//60:02d}:{hh%60:02d}): mean_amp={seg.mean():.2f}")

    # === de-meaned amp / forward_amp using the pooled session-shape curve ===
    def expected_forward(i, h):
        seg = shape[i + 1 : i + 1 + h]
        seg = seg[~np.isnan(seg)]
        return seg.mean() if len(seg) else np.nan

    print("\n=== Test 1b: raw vs time-of-day-adjusted persistence IC ===")
    print(f"{'h':>4} {'raw_IC':>8} {'raw_t':>7} {'raw_p':>8} | {'anomaly_IC':>10} {'anom_t':>7} {'anom_p':>8}")
    for h in (12, 35, 60, 90, 120, 180):
        raw_ics, anom_ics = [], []
        for date, s in series.items():
            wamp, n = s["wamp"], s["n"]
            famp = forward_amp(s["bars"], h)
            for arr, store in ((wamp, None),):
                pass
            valid_idx = [i for i in range(n) if not np.isnan(wamp[i]) and not np.isnan(famp[i]) and i < max_n]
            if len(valid_idx) < 20:
                continue
            x_raw = np.array([wamp[i] for i in valid_idx])
            y_raw = np.array([famp[i] for i in valid_idx])
            x_anom = np.array([wamp[i] - shape[i] for i in valid_idx])
            y_anom = np.array([famp[i] - expected_forward(i, h) for i in valid_idx])
            ok = ~(np.isnan(x_anom) | np.isnan(y_anom))
            if ok.sum() < 20:
                continue
            ic_raw, _ = sstats.spearmanr(x_raw[ok], y_raw[ok])
            ic_anom, _ = sstats.spearmanr(x_anom[ok], y_anom[ok])
            raw_ics.append(ic_raw)
            anom_ics.append(ic_anom)
        rm, rt, rp = day_clustered(raw_ics)
        am, at, ap = day_clustered(anom_ics)
        print(f"{h:>4} {rm:>8.4f} {rt:>7.2f} {rp:>8.4f} | {am:>10.4f} {at:>7.2f} {ap:>8.4f}")

    # === Test 2: trend strength |slope| ===
    print("\n=== Test 2: trend strength |slope30| ===")
    print(f"{'h':>4} {'contemp corr(amp,|slope|)':>26} | {'IC(|slope|,fwd_amp)':>20} t p | {'IC(amp_resid_on_|slope|, fwd_amp)':>34}")
    for h in (12, 35, 90):
        contemp, ic_slope_fwd, ic_resid_fwd = [], [], []
        for date, s in series.items():
            wamp, slope, n = s["wamp"], s["slope"], s["n"]
            famp = forward_amp(s["bars"], h)
            valid = ~(np.isnan(wamp) | np.isnan(slope) | np.isnan(famp))
            if valid.sum() < 20:
                continue
            a, sl, y = wamp[valid], np.abs(slope[valid]), famp[valid]
            c, _ = sstats.spearmanr(a, sl)
            contemp.append(c)
            icf, _ = sstats.spearmanr(sl, y)
            ic_slope_fwd.append(icf)
            # residualize amp on |slope| (linear), correlate residual with forward amp
            b = np.polyfit(sl, a, 1)
            resid = a - np.polyval(b, sl)
            icr, _ = sstats.spearmanr(resid, y)
            ic_resid_fwd.append(icr)
        cm, ct, cp = day_clustered(contemp)
        sm_, st, sp = day_clustered(ic_slope_fwd)
        rm, rt, rp = day_clustered(ic_resid_fwd)
        print(f"{h:>4} corr={cm:.3f}(t={ct:.2f},p={cp:.4f}) | slope->fwd IC={sm_:.4f}(t={st:.2f},p={sp:.4f}) | amp-resid(on slope)->fwd IC={rm:.4f}(t={rt:.2f},p={rp:.4f})")

    # === Test 3: price direction (sign of slope) ===
    print("\n=== Test 3: does persistence differ between up-trend vs down-trend regimes? ===")
    print(f"{'h':>4} {'IC | slope>0 (uptrend)':>24} {'IC | slope<0 (downtrend)':>26} {'IC(sign(slope), surprise)':>26}")
    for h in (12, 35, 90):
        ic_up, ic_down, ic_sign_surprise = [], [], []
        for date, s in series.items():
            wamp, slope, n = s["wamp"], s["slope"], s["n"]
            famp = forward_amp(s["bars"], h)
            valid = ~(np.isnan(wamp) | np.isnan(slope) | np.isnan(famp))
            if valid.sum() < 20:
                continue
            a, sl, y = wamp[valid], slope[valid], famp[valid]
            surprise = y - a
            up = sl > 0
            down = sl < 0
            if up.sum() >= 15:
                icu, _ = sstats.spearmanr(a[up], y[up])
                ic_up.append(icu)
            if down.sum() >= 15:
                icd, _ = sstats.spearmanr(a[down], y[down])
                ic_down.append(icd)
            ics, _ = sstats.spearmanr(np.sign(sl), surprise)
            ic_sign_surprise.append(ics)
        um, ut, up_ = day_clustered(ic_up)
        dm, dt, dp = day_clustered(ic_down)
        sm2, st2, sp2 = day_clustered(ic_sign_surprise)
        print(f"{h:>4} {um:.4f}(t={ut:.2f},p={up_:.4f}){'':>4} {dm:.4f}(t={dt:.2f},p={dp:.4f}){'':>6} {sm2:.4f}(t={st2:.2f},p={sp2:.4f})")


if __name__ == "__main__":
    main()
