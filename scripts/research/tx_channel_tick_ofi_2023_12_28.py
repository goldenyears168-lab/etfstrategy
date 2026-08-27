#!/usr/bin/env python3
"""Tick-rule OFI proxy IC check for 2023-12-28 (day session only).

Classic tick rule (Lee-Ready predecessor): tick classified buyer-initiated
(+volume) if price > prev tick price, seller-initiated (-volume) if price <
prev tick price, carry-forward previous classification on zero-tick.
Rolling 60 real-second signed-volume sum computed causally at each tick.
IC = correlation(OFI_t, fwd_return[t, t+h]) for h in {1,3,5} minutes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tx_channel_tick_validation import load_front_month_ticks  # noqa: E402

DAY = "2023-12-28"


def main() -> None:
    df = load_front_month_ticks(DAY)
    if df is None or df.empty:
        print(f"FAIL: no ticks loaded for {DAY}")
        return

    df = df.reset_index(drop=True)
    n_raw = len(df)

    # Day-session filter (08:45-13:45) to avoid night-session concatenation edge cases.
    t = df["dt"].dt.time
    mask = (t >= pd.Timestamp("08:45:00").time()) & (t <= pd.Timestamp("13:45:00").time())
    df = df[mask].reset_index(drop=True)
    n_day = len(df)

    price = df["price"].to_numpy(dtype=float)
    vol = df["volume"].to_numpy(dtype=float)
    dt = df["dt"].to_numpy()

    # Tick rule classification.
    sign = np.zeros(len(df), dtype=float)
    sign[0] = 1.0  # arbitrary seed for first tick
    for i in range(1, len(df)):
        if price[i] > price[i - 1]:
            sign[i] = 1.0
        elif price[i] < price[i - 1]:
            sign[i] = -1.0
        else:
            sign[i] = sign[i - 1]
    signed_vol = sign * vol

    dt_s = df["dt"]
    # Rolling 60s signed-volume sum, strictly causal (window [t-60s, t]).
    sv_series = pd.Series(signed_vol, index=pd.DatetimeIndex(dt_s))
    ofi = sv_series.rolling("60s", closed="both").sum().to_numpy()

    # Forward returns via searchsorted on time (next tick at/after t+h).
    dt_ns = dt_s.astype("int64").to_numpy()
    results = {}
    for horizon_min, label in [(1, "1min"), (3, "3min"), (5, "5min")]:
        target_ns = dt_ns + horizon_min * 60 * 1_000_000_000
        idx = np.searchsorted(dt_ns, target_ns, side="left")
        valid = idx < len(dt_ns)
        fwd_ret = np.full(len(df), np.nan)
        fwd_ret[valid] = price[idx[valid]] - price[np.arange(len(df))[valid]]
        results[label] = fwd_ret

    out = pd.DataFrame({"ofi": ofi})
    for label, fwd in results.items():
        out[f"fwd_{label}"] = fwd

    out_clean = out.dropna()
    print(f"day={DAY} n_raw_ticks={n_raw} n_day_session_ticks={n_day} n_valid_rows={len(out_clean)}")

    for label in ["1min", "3min", "5min"]:
        sub = out_clean[["ofi", f"fwd_{label}"]].dropna()
        if len(sub) < 30:
            print(f"{label}: insufficient n={len(sub)}")
            continue
        pearson = sub["ofi"].corr(sub[f"fwd_{label}"], method="pearson")
        spearman = sub["ofi"].corr(sub[f"fwd_{label}"], method="spearman")
        print(f"{label}: n={len(sub)} pearson_IC={pearson:.4f} spearman_IC={spearman:.4f}")

    # basic sanity on OFI distribution
    print(f"OFI stats: mean={np.nanmean(ofi):.2f} std={np.nanstd(ofi):.2f} "
          f"min={np.nanmin(ofi):.2f} max={np.nanmax(ofi):.2f}")
    print(f"vol stats: mean={np.nanmean(vol):.3f} sum={np.nansum(vol):.1f}")


if __name__ == "__main__":
    main()
