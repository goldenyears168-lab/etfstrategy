#!/usr/bin/env python3
"""Tick-rule OFI proxy IC test — single-day assignment (2024-07-08).

Classifies each tick as buyer/seller-initiated via the tick rule (Lee-Ready
substitute, no bid/ask available in historical tick cache), computes a
strictly-causal rolling 60-real-second signed-volume sum at each tick, and
correlates it against forward returns at 1/3/5 minute horizons.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tx_channel_tick_validation import load_front_month_ticks, DAY_SESSION_START, DAY_SESSION_END  # noqa: E402

DAY = "2024-07-08"


def main() -> None:
    df = load_front_month_ticks(DAY)
    if df is None or df.empty:
        print(f"NO DATA for {DAY}")
        return

    df = df.set_index("dt").between_time(DAY_SESSION_START, DAY_SESSION_END).reset_index()
    df = df.sort_values("dt").reset_index(drop=True)
    n_raw = len(df)

    price = df["price"].to_numpy(dtype=float)
    vol = df["volume"].to_numpy(dtype=float)
    t = df["dt"].to_numpy()

    # Tick rule: +1 buyer-initiated, -1 seller-initiated, carry forward on zero-tick.
    sign = np.zeros(n_raw)
    sign[0] = 1.0  # arbitrary init for first tick
    for i in range(1, n_raw):
        if price[i] > price[i - 1]:
            sign[i] = 1.0
        elif price[i] < price[i - 1]:
            sign[i] = -1.0
        else:
            sign[i] = sign[i - 1]
    signed_vol = sign * vol

    # Strictly causal rolling 60-real-second signed-volume sum via searchsorted on time.
    t64 = t.astype("datetime64[ns]").astype(np.int64)
    window_ns = 60_000_000_000  # 60s in ns
    cumsum = np.concatenate([[0.0], np.cumsum(signed_vol)])
    lo_idx = np.searchsorted(t64, t64 - window_ns, side="left")
    ofi = cumsum[np.arange(n_raw) + 1] - cumsum[lo_idx]

    results = {}
    for label, horizon_s in [("1min", 60), ("3min", 180), ("5min", 300)]:
        horizon_ns = horizon_s * 1_000_000_000
        target_t = t64 + horizon_ns
        fwd_idx = np.searchsorted(t64, target_t, side="left")
        valid = fwd_idx < n_raw
        x = ofi[valid]
        y = price[fwd_idx[valid]] - price[valid]
        n = len(x)
        if n < 30:
            results[label] = (np.nan, np.nan, n)
            continue
        rho, p_s = stats.spearmanr(x, y)
        r, p_p = stats.pearsonr(x, y)
        results[label] = (rho, r, n, p_s, p_p)

    print(f"day={DAY} n_raw_ticks={n_raw} (day-session {DAY_SESSION_START}-{DAY_SESSION_END})")
    for label, res in results.items():
        if len(res) == 3:
            print(f"  {label}: insufficient n={res[2]}")
        else:
            rho, r, n, p_s, p_p = res
            print(f"  {label}: n={n} spearman_IC={rho:.4f} (p={p_s:.4g}) pearson_IC={r:.4f} (p={p_p:.4g})")


if __name__ == "__main__":
    main()
