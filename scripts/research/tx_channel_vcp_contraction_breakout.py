"""Ad-hoc: does a genuine volatility-contraction phase (VCP-style: short-term
ATR well below long-term ATR) before a local high/low breakout make that
breakout more reliable than a random breakout with no contraction behind it?

Detection: cr[t] = ATR(10)/ATR(50). in_contraction[t] = cr[t] below a CAUSAL
rolling percentile threshold (learned only from trailing history — the round-8
lesson in this thread: a global/look-ahead threshold inflates results).

Breakout event: close clears the trailing 20-bar high (bullish) or trailing
20-bar low (bearish). Split events into "VCP-preceded" (contraction detected
at some point in the trailing 20 bars) vs "no-contraction" breakouts, compare
forward returns (in breakout direction) at several horizons, day-clustered.

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
from tx_channel_amp_persistence_drivers import day_clustered

LOOKBACK_BARS = 20
WARMUP_DAYS = 20
ROLL_WINDOW = 40  # trading days of trailing history for the causal contraction threshold
HORIZONS = [5, 12, 25, 35]


def atr_series(H, L, C, period):
    n = len(H)
    tr = np.full(n, np.nan)
    for i in range(1, n):
        tr[i] = max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1]))
    atr = np.full(n, np.nan)
    for i in range(period, n):
        atr[i] = np.nanmean(tr[i - period + 1 : i + 1])
    return atr


def compression_ratio(H, L, C, short=10, long=50):
    atr_s = atr_series(H, L, C, short)
    atr_l = atr_series(H, L, C, long)
    with np.errstate(invalid="ignore", divide="ignore"):
        cr = atr_s / atr_l
    return cr


def find_breakouts(H, L, C, cr, contraction_thresh):
    """Returns list of (bar_idx, direction) for local-high/low breakouts,
    plus whether a contraction was detected in the trailing LOOKBACK_BARS."""
    n = len(C)
    events = []
    for t in range(60, n):
        local_high = np.nanmax(H[t - LOOKBACK_BARS : t])
        local_low = np.nanmin(L[t - LOOKBACK_BARS : t])
        was_contracted = np.any(cr[t - LOOKBACK_BARS : t] <= contraction_thresh)
        direction = None
        if C[t] > local_high:
            direction = 1
        elif C[t] < local_low:
            direction = -1
        if direction is not None:
            events.append((t, direction, bool(was_contracted)))
    return events


def main():
    bars_by_day = load_all_day_bars()
    dates = sorted(bars_by_day)
    print(f"n days = {len(dates)}")

    series = {}
    for d in dates:
        bars = bars_by_day[d]
        H = bars["h"].to_numpy()
        L = bars["l"].to_numpy()
        C = bars["c"].to_numpy()
        cr = compression_ratio(H, L, C)
        series[d] = dict(H=H, L=L, C=C, cr=cr)

    for h in HORIZONS:
        vcp_by_day, non_by_day = {}, {}
        n_vcp, n_non = 0, 0
        for i, d in enumerate(dates):
            if i < WARMUP_DAYS:
                continue
            prior = dates[max(0, i - ROLL_WINDOW) : i]
            prior_cr = np.concatenate([series[p]["cr"][~np.isnan(series[p]["cr"])] for p in prior])
            if len(prior_cr) < 300:
                continue
            thresh = np.percentile(prior_cr, 33)

            s = series[d]
            H, L, C, cr = s["H"], s["L"], s["C"], s["cr"]
            events = find_breakouts(H, L, C, cr, thresh)
            n = len(C)
            vcp_rets, non_rets = [], []
            for t, direction, was_contracted in events:
                if t + h >= n:
                    continue
                fwd = direction * (C[t + h] - C[t])
                if was_contracted:
                    vcp_rets.append(fwd)
                else:
                    non_rets.append(fwd)
            if vcp_rets:
                vcp_by_day[d] = np.mean(vcp_rets)
                n_vcp += len(vcp_rets)
            if non_rets:
                non_by_day[d] = np.mean(non_rets)
                n_non += len(non_rets)

        vm, vt, vp = day_clustered(list(vcp_by_day.values()))
        nm, nt, npp = day_clustered(list(non_by_day.values()))
        print(f"\nh={h}min:")
        print(f"  VCP前置(有先收縮再突破)  n_events={n_vcp:>5} n_days={len(vcp_by_day):>3}  mean_fwd_pts={vm:>7.2f}  t={vt:>6.2f}  p={vp:.4f}")
        print(f"  無前置收縮(隨機突破)     n_events={n_non:>5} n_days={len(non_by_day):>3}  mean_fwd_pts={nm:>7.2f}  t={nt:>6.2f}  p={npp:.4f}")
        common_days = set(vcp_by_day) & set(non_by_day)
        if common_days:
            diffs = [vcp_by_day[d] - non_by_day[d] for d in common_days]
            dm, dtt, dpp = day_clustered(diffs)
            print(f"  差異(VCP - 無前置)，僅同時有兩種事件的{len(common_days)}天：mean={dm:.2f}  t={dtt:.2f}  p={dpp:.4f}")


if __name__ == "__main__":
    main()
