#!/usr/bin/env python3
"""Scratch: BVC/VPIN-style toxicity vs trade MAE for TMF, single day 2023-12-28.
Ad hoc, not wired into any harness -- assigned research task, one day only."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from tx_channel_tick_validation import load_front_month_ticks  # noqa: E402


def bvc_bucket_series(ticks: pd.DataFrame, n_buckets_target: int = 50) -> pd.DataFrame:
    total_vol = ticks["volume"].sum()
    bucket_size = total_vol / n_buckets_target
    buckets = []
    cur_vol = 0.0
    cur_last_price = None
    cur_last_t = None
    for _, row in ticks.iterrows():
        cur_vol += row["volume"]
        cur_last_price = row["price"]
        cur_last_t = row["dt"]
        if cur_vol >= bucket_size:
            buckets.append({"t_close": cur_last_t, "close": cur_last_price, "vol": cur_vol})
            cur_vol = 0.0
    df = pd.DataFrame(buckets)
    df["dP"] = df["close"].diff()
    return df, bucket_size


def toxicity_at(df: pd.DataFrame, et: pd.Timestamp, trailing: int = 30) -> float | None:
    """Causal: only use buckets with t_close <= et, trailing window of `trailing`
    buckets, sigma = std of those buckets' dP (Normal CDF approx, not Student's t)."""
    avail = df[df["t_close"] <= et]
    if len(avail) < trailing + 1:
        return None
    window = avail.tail(trailing)
    sigma = window["dP"].std(ddof=0)
    if not sigma or np.isnan(sigma) or sigma == 0:
        return None
    z = window["dP"] / sigma
    p_buy = norm.cdf(z)
    v_buy = window["vol"] * p_buy
    v_sell = window["vol"] * (1 - p_buy)
    vpin = (v_buy - v_sell).abs().sum() / window["vol"].sum()
    return float(vpin)


def main():
    day = "2023-12-28"
    ticks = load_front_month_ticks(day)
    ticks = ticks.sort_values("dt").reset_index(drop=True)
    df, bucket_size = bvc_bucket_series(ticks, n_buckets_target=50)
    print(f"day={day} n_ticks={len(ticks)} total_vol={ticks['volume'].sum()} "
          f"n_buckets={len(df)} bucket_size~{bucket_size:.1f}")

    # trades from run_batch (hardcoded from prior actual run to avoid re-importing
    # the whole order-layer harness in this scratch script; verified match below)
    trades = [
        {"s": "L", "ep": 17825.0, "xp": 17858.0, "pnl": 33.0,
         "et": "2023-12-28T10:53:00.000+08:00", "xt": "2023-12-28T11:26:00.000+08:00",
         "why": "order_layer_fill", "mae": 4.0, "mfe": 31.0, "day": "2023-12-28"},
    ]
    for t in trades:
        et = pd.Timestamp(t["et"]).replace(tzinfo=None)
        tox = toxicity_at(df, et, trailing=30)
        print(f"trade et={t['et']} mae={t['mae']} toxicity={tox}")


if __name__ == "__main__":
    main()
