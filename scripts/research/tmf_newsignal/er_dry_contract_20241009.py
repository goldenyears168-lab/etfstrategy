#!/usr/bin/env python3
"""Kaufman ER within PV8 dry/contract states — single-day test, 2024-10-09.

1-minute causal bars (day session 08:45-13:45), built from real tick data via
load_front_month_ticks(). classify_pv/rvol_series imported directly from
src/tmf_channel/causal_engine.py (not reimplemented). At every bar where
classify_pv returns "dry" or "contract", compute trailing-N=20-bar Kaufman ER
and split into top/bottom tercile. For each group, compute forward return at
5/10/15 minutes in the direction ER's own net-motion sign predicts, and report
hit rate + average signed forward-return magnitude.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path("/Users/jackm4/goldenstocks/src")))
sys.path.insert(0, str(Path("/Users/jackm4/goldenstocks/scripts/research")))

from tmf_channel.causal_engine import classify_pv, rvol_series  # noqa: E402
from tx_channel_tick_validation import load_front_month_ticks, resample_to_1min  # noqa: E402

DAY = "2024-10-09"
N_ER = 20


def kaufman_er(C: list[float], t: int, n: int = N_ER) -> float | None:
    a = t - n
    if a < 0:
        return None
    net = abs(C[t] - C[a])
    denom = sum(abs(C[i] - C[i - 1]) for i in range(a + 1, t + 1))
    if denom <= 0:
        return None
    return net / denom


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    if ticks is None or ticks.empty:
        print("NO TICKS")
        return
    bars = resample_to_1min(ticks)
    if bars.empty:
        print("NO BARS")
        return
    bars = bars.reset_index(drop=True)
    C = bars["Close"].tolist()
    O = bars["Open"].tolist()
    V = bars["Volume"].tolist()
    n_bars = len(bars)
    print(f"day={DAY} n_1min_bars={n_bars} (day session 08:45-13:45 only)")

    rvol = rvol_series(V)

    # collect dry/contract flagged bars with ER + direction
    recs = []
    for t in range(n_bars):
        state, impulse = classify_pv(C, O, rvol, t, look=5)
        if state not in ("dry", "contract"):
            continue
        er = kaufman_er(C, t, N_ER)
        if er is None:
            continue
        a = t - N_ER
        net_dir = 1 if C[t] - C[a] > 0 else (-1 if C[t] - C[a] < 0 else 0)
        if net_dir == 0:
            continue
        recs.append(dict(t=t, state=state, er=er, dir=net_dir))

    print(f"n_dry_contract_bars_with_ER={len(recs)}")
    if len(recs) < 10:
        print("TOO FEW — insufficient dry/contract periods with valid ER on this single day")
        return

    ers = sorted(r["er"] for r in recs)
    n = len(ers)
    lo_cut = ers[n // 3]
    hi_cut = ers[(2 * n) // 3]
    low_group = [r for r in recs if r["er"] <= lo_cut]
    high_group = [r for r in recs if r["er"] >= hi_cut]
    print(f"ER tercile cuts: lo<={lo_cut:.4f} hi>={hi_cut:.4f}")
    print(f"n_low_er={len(low_group)} n_high_er={len(high_group)}")

    horizons_min = [5, 10, 15]
    results = {}
    for label, group in [("LOW_ER", low_group), ("HIGH_ER", high_group)]:
        for h in horizons_min:
            hits = []
            mags = []
            for r in group:
                t2 = r["t"] + h
                if t2 >= n_bars:
                    continue
                fwd = C[t2] - C[r["t"]]
                signed = fwd * r["dir"]
                hits.append(1 if signed > 0 else 0)
                mags.append(signed)
            if mags:
                hit_rate = float(np.mean(hits))
                avg_mag = float(np.mean(mags))
                n_eval = len(mags)
            else:
                hit_rate = float("nan")
                avg_mag = float("nan")
                n_eval = 0
            results[(label, h)] = (hit_rate, avg_mag, n_eval)
            print(f"{label} h={h}min: n={n_eval} hit_rate={hit_rate:.3f} avg_signed_fwd_ret={avg_mag:+.2f}pt")

    print()
    for h in horizons_min:
        hr_low, mag_low, n_low = results[("LOW_ER", h)]
        hr_high, mag_high, n_high = results[("HIGH_ER", h)]
        print(f"h={h}: HIGH_ER hit={hr_high:.3f} mag={mag_high:+.2f} | LOW_ER hit={hr_low:.3f} mag={mag_low:+.2f} | "
              f"HIGH>LOW hit? {hr_high > hr_low if not np.isnan(hr_high) and not np.isnan(hr_low) else 'n/a'}")


if __name__ == "__main__":
    main()
