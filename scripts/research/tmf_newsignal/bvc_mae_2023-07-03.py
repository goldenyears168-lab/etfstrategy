#!/usr/bin/env python3
"""BVC/VPIN-style toxicity vs MAE magnitude, single-day probe for 2023-07-03.

Detective angle (2026-08-13): the earlier tick-rule Order Flow Imbalance test
died because per-tick direction classification is corrupted by bid-ask
bounce, and it targeted DIRECTION (return sign) not MAGNITUDE -- this repo's
own research has repeatedly found direction has ~no edge but magnitude/path
might. BVC (Bulk Volume Classification, Easley/Lopez de Prado/O'Hara VPIN)
avoids the per-tick bounce problem: it buckets EQUAL VOLUME into bins and
classifies each bin's buy/sell split via the *standardized bin return*
through a CDF (we use a Normal CDF approximation here, NOT a fitted
t-distribution -- explicitly noted per task instructions).

Pipeline:
  1. Load raw ticks for the day (price+volume+timestamp only, no bid/ask) via
     tx_channel_tick_validation.load_front_month_ticks().
  2. Build causal equal-volume buckets, bucket_size ~= total_day_volume/50.
  3. For each bucket: z = (close-open) / sigma, sigma = expanding/trailing
     std of prior bucket price changes (causal, excludes current bucket).
     buy_frac = NormalCDF(z), sell_frac = 1-buy_frac.
  4. Toxicity(bucket i) = mean(|buy-sell| volume fraction) over the trailing
     30 buckets ending at bucket i (causal, VPIN-style).
  5. Run run_batch(['2023-07-03']) with the live baseline recipe
     (specialized_cell_book(), ORDER_TMF_CHANNEL_NIGHT_USES_DAY_RECIPE=1) to
     get real trades with mae/mfe.
  6. asof-join each trade's entry time (et) to the most recently CLOSED
     toxicity bucket (no lookahead) and report the (toxicity, mae) pairs +
     Spearman correlation.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

import numpy as np
import pandas as pd

from tx_channel_tick_validation import load_front_month_ticks  # noqa: E402

DAY = "2023-07-03"
N_BUCKETS_TARGET = 50
TRAILING_WINDOW = 30


def normal_cdf(z: np.ndarray) -> np.ndarray:
    from math import erf, sqrt
    return 0.5 * (1.0 + np.vectorize(lambda x: erf(x / sqrt(2.0)))(z))


def build_equal_volume_buckets(ticks: pd.DataFrame, n_buckets_target: int) -> pd.DataFrame:
    """Causal equal-volume bucketing. No splitting of a single tick's volume
    across a bucket boundary (ticks are small relative to bucket size here,
    checked below) -- a bucket closes on the first tick that pushes
    cumulative volume >= bucket_size."""
    total_vol = float(ticks["volume"].sum())
    bucket_size = total_vol / n_buckets_target
    rows = []
    cum = 0.0
    open_price = float(ticks["price"].iloc[0])
    for _, r in ticks.iterrows():
        cum += float(r["volume"])
        if cum >= bucket_size:
            rows.append(dict(t=r["dt"], open=open_price, close=float(r["price"]), volume=cum))
            cum = 0.0
            open_price = float(r["price"])
    if cum > 0:
        rows.append(dict(t=ticks["dt"].iloc[-1], open=open_price,
                          close=float(ticks["price"].iloc[-1]), volume=cum))
    df = pd.DataFrame(rows)
    df["dP"] = df["close"] - df["open"]
    return df, bucket_size


def add_bvc_toxicity(buckets: pd.DataFrame, trailing_window: int) -> pd.DataFrame:
    buckets = buckets.copy()
    # causal sigma: expanding std of PRIOR buckets' dP (min 5 obs to start),
    # excludes current bucket to avoid using its own realized move as its
    # own classifier scale.
    prior_std = buckets["dP"].expanding().std().shift(1)
    sigma = prior_std.bfill()  # first few buckets: backfill from earliest available std
    z = (buckets["dP"] / sigma.replace(0, np.nan)).fillna(0.0).to_numpy()
    buy_frac = normal_cdf(z)
    buckets["buy_vol"] = buy_frac * buckets["volume"]
    buckets["sell_vol"] = (1 - buy_frac) * buckets["volume"]
    buckets["imbalance_frac"] = (buckets["buy_vol"] - buckets["sell_vol"]).abs() / buckets["volume"]
    buckets["toxicity"] = buckets["imbalance_frac"].rolling(trailing_window, min_periods=5).mean()
    return buckets


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    assert ticks is not None and not ticks.empty, "no ticks loaded"
    print(f"n_ticks={len(ticks)} total_volume={ticks['volume'].sum()}", file=sys.stderr)

    buckets, bucket_size = build_equal_volume_buckets(ticks, N_BUCKETS_TARGET)
    print(f"n_buckets={len(buckets)} bucket_size~={bucket_size:.1f}", file=sys.stderr)
    max_tick_vol = ticks["volume"].max()
    print(f"max_single_tick_volume={max_tick_vol} (bucket_size={bucket_size:.1f}, "
          f"ratio={max_tick_vol/bucket_size:.3f} -- no cross-bucket volume splitting done)",
          file=sys.stderr)

    buckets = add_bvc_toxicity(buckets, TRAILING_WINDOW)

    from tmf_walkforward_harness import run_batch
    from order.tmf_channel_pv16_book import specialized_cell_book

    import os
    os.environ["ORDER_TMF_CHANNEL_NIGHT_USES_DAY_RECIPE"] = "1"
    book = specialized_cell_book()
    result = run_batch([DAY], session_pv_book=book, label="bvc-mae-2023-07-03")
    trades = result["trades"]
    print(f"n_trades={len(trades)}", file=sys.stderr)

    tox_series = buckets.set_index("t")["toxicity"].dropna().sort_index()

    pairs = []
    for tr in trades:
        et = pd.Timestamp(tr["et"]).tz_localize(None)
        # most recently CLOSED bucket strictly at/before entry time (causal)
        idx = tox_series.index.searchsorted(et, side="right") - 1
        if idx < 0:
            tox = None
        else:
            tox = float(tox_series.iloc[idx])
        pairs.append(dict(et=tr["et"], side=tr["s"], toxicity=tox, mae=tr["mae"], pnl=tr["pnl"]))

    valid = [p for p in pairs if p["toxicity"] is not None]
    print("\n--- pairs ---")
    for p in pairs:
        print(p)

    if len(valid) >= 3:
        tox_vals = [p["toxicity"] for p in valid]
        mae_vals = [p["mae"] for p in valid]
        rho = pd.Series(tox_vals).corr(pd.Series(mae_vals), method="spearman")
        print(f"\nSpearman(toxicity, mae) n={len(valid)}: {rho}")
    else:
        print(f"\nn_valid={len(valid)} < 3 -- correlation not meaningful, not computed")


if __name__ == "__main__":
    main()
