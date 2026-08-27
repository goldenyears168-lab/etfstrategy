#!/usr/bin/env python3
"""OFI (tick-rule signed-volume proxy) IC check, single day 2023-09-28.

Fresh test tonight: rolling 60-real-second signed-volume (tick rule classification)
vs forward return at 1/3/5 min horizons. Strictly causal.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import pearsonr, spearmanr

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tx_channel_tick_validation import load_front_month_ticks, DAY_SESSION_START, DAY_SESSION_END  # noqa: E402

DAY = "2023-09-28"


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    if ticks is None or ticks.empty:
        print("LOAD_FAILED: no ticks returned")
        return

    ticks = ticks.set_index("dt").between_time(DAY_SESSION_START, DAY_SESSION_END).reset_index()
    ticks = ticks.sort_values("dt").reset_index(drop=True)
    n_raw = len(ticks)
    if n_raw < 100:
        print(f"TOO_SPARSE: only {n_raw} ticks in day session")
        return

    price = ticks["price"].to_numpy(dtype=float)
    vol = ticks["volume"].to_numpy(dtype=float)
    ts = ticks["dt"].to_numpy()

    # tick rule classification: +1 buyer-initiated, -1 seller-initiated, carry fwd on tie
    direction = np.zeros(n_raw, dtype=float)
    direction[0] = 1.0  # arbitrary seed for first tick
    for i in range(1, n_raw):
        if price[i] > price[i - 1]:
            direction[i] = 1.0
        elif price[i] < price[i - 1]:
            direction[i] = -1.0
        else:
            direction[i] = direction[i - 1]
    signed_vol = direction * vol

    # rolling 60 real-second signed volume sum, strictly causal (window = (t-60s, t])
    ts_s = ticks["dt"].astype("int64").to_numpy() / 1e9  # seconds since epoch
    cum_signed = np.concatenate([[0.0], np.cumsum(signed_vol)])
    ofi60 = np.empty(n_raw)
    left = 0
    for i in range(n_raw):
        cutoff = ts_s[i] - 60.0
        while ts_s[left] <= cutoff:
            left += 1
        # window is ticks[left..i] inclusive -> cum_signed[i+1]-cum_signed[left]
        ofi60[i] = cum_signed[i + 1] - cum_signed[left]

    # forward returns: price at next available tick at/after t+horizon, minus price at t
    def forward_return(horizon_s: float) -> np.ndarray:
        fwd = np.full(n_raw, np.nan)
        j = 0
        for i in range(n_raw):
            target = ts_s[i] + horizon_s
            if j < i:
                j = i
            while j < n_raw and ts_s[j] < target:
                j += 1
            if j < n_raw:
                fwd[i] = price[j] - price[i]
            else:
                fwd[i] = np.nan
        return fwd

    fwd1 = forward_return(60.0)
    fwd3 = forward_return(180.0)
    fwd5 = forward_return(300.0)

    results = {}
    for label, fwd in [("1min", fwd1), ("3min", fwd3), ("5min", fwd5)]:
        mask = ~np.isnan(fwd)
        x = ofi60[mask]
        y = fwd[mask]
        n = len(x)
        if n < 30:
            results[label] = (np.nan, np.nan, n)
            continue
        pear, _ = pearsonr(x, y)
        spear, _ = spearmanr(x, y)
        results[label] = (pear, spear, n)

    print(f"day={DAY} n_ticks_day_session={n_raw}")
    for label, (pear, spear, n) in results.items():
        print(f"{label}: n={n} pearson_IC={pear:.5f} spearman_IC={spear:.5f}")


if __name__ == "__main__":
    main()
