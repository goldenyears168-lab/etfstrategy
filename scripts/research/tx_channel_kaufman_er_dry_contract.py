#!/usr/bin/env python3
"""Kaufman Efficiency Ratio (ER) as a grinding-decline/incline detector, tested
INSIDE PV8's dry/contract states (low realized vol). Orthogonal to rvol by
construction: ER measures net-directional-motion / total-path-length over a
trailing window, bounded [0,1]. Single-day causal test, day session only.

Assigned day: 2023-12-28. Bars: 1-minute (chosen because N=20-bar trailing ER
window + 5/10/15-min forward horizons need real elapsed time; 1s bars would
make a 20-bar ER window span only 20 seconds -- too short to capture a "grind"
and mostly re-measures microstructure noise, not drift).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_tick_validation import load_front_month_ticks, resample_to_1min  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from tmf_channel.causal_engine import classify_pv, rvol_series  # noqa: E402

DAY = "2023-12-28"
N_ER = 20


def kaufman_er(C: np.ndarray, t: int, n: int = N_ER) -> float | None:
    a = t - n
    if a < 0:
        return None
    window = C[a : t + 1]
    net = window[-1] - window[0]
    path = np.abs(np.diff(window)).sum()
    if path <= 0:
        return None
    return net / path  # signed: sign = ER's predicted direction


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    if ticks is None or ticks.empty:
        print(f"NO TICK DATA for {DAY}")
        return
    bars = resample_to_1min(ticks)
    if bars.empty:
        print(f"NO BARS resampled for {DAY}")
        return
    bars = bars.reset_index(drop=True)
    C = bars["Close"].to_numpy(float)
    O = bars["Open"].to_numpy(float)
    V = bars["Volume"].to_numpy(float)
    n_bars = len(bars)

    rvol = rvol_series(list(V))

    states = []
    ers = []
    for t in range(n_bars):
        state, _impulse = classify_pv(list(C), list(O), rvol, t, look=5)
        states.append(state)
        ers.append(kaufman_er(C, t, N_ER))

    df = pd.DataFrame({"t": range(n_bars), "state": states, "er": ers, "close": C})
    dry_contract = df[df["state"].isin(["dry", "contract"])].dropna(subset=["er"]).copy()
    n_dc = len(dry_contract)
    print(f"day={DAY} n_bars={n_bars} n_dry_contract_with_er={n_dc}")
    if n_dc < 15:
        print("TOO FEW dry/contract+ER periods for tercile split -- reporting raw only.")

    dry_contract["abs_er"] = dry_contract["er"].abs()
    q_lo, q_hi = dry_contract["abs_er"].quantile([1 / 3, 2 / 3])
    low = dry_contract[dry_contract["abs_er"] <= q_lo]
    high = dry_contract[dry_contract["abs_er"] >= q_hi]
    print(f"tercile cutoffs (abs ER): low<={q_lo:.4f} high>={q_hi:.4f}")
    print(f"n_low_er={len(low)} n_high_er={len(high)}")

    def fwd_stats(group: pd.DataFrame, horizon_min: int) -> tuple[float | None, float | None, int]:
        rets = []
        hits = []
        for _, row in group.iterrows():
            t = int(row["t"])
            tt = t + horizon_min
            if tt >= n_bars:
                continue
            er_sign = np.sign(row["er"])
            if er_sign == 0:
                continue
            raw_ret = C[tt] - C[t]
            signed_ret = raw_ret * er_sign
            rets.append(signed_ret)
            hits.append(1 if signed_ret > 0 else 0)
        if not rets:
            return None, None, 0
        return float(np.mean(rets)), float(np.mean(hits)), len(rets)

    print("\nhorizon(min) | group | n | mean_signed_fwd_ret(pt) | hit_rate")
    results = {}
    for hz in (5, 10, 15):
        for name, grp in (("HIGH_ER", high), ("LOW_ER", low)):
            mean_ret, hit_rate, n = fwd_stats(grp, hz)
            results[(hz, name)] = (mean_ret, hit_rate, n)
            print(f"{hz:>12} | {name:>7} | {n:>3} | "
                  f"{'NA' if mean_ret is None else round(mean_ret, 2):>10} | "
                  f"{'NA' if hit_rate is None else round(hit_rate, 3)}")

    print("\n--- summary (15min) ---")
    print(results.get((15, "HIGH_ER")), results.get((15, "LOW_ER")))


if __name__ == "__main__":
    main()
