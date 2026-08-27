#!/usr/bin/env python3
"""One-off (detective-follow-up, single-day n<=10 test): does BVC/VPIN-style
toxicity at trade ENTRY time predict MAE MAGNITUDE for that trade? Day =
2023-09-28 (assigned). Scratch script, not wired into any harness -- see
chat report for the actual finding; keep for reproducibility only."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm, spearmanr

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

DAY = "2023-09-28"
TICK_PATH = Path(
    f"/Users/jackm4/goldenstocks-data/cache/tmf_channel/finmind_tx_tick_by_day/{DAY}.json"
)


def load_ticks(day: str) -> pd.DataFrame:
    rows = json.loads(TICK_PATH.read_text())
    df = pd.DataFrame(rows)
    df = df[~df["contract_date"].str.contains("/")]
    front = df["contract_date"].value_counts().idxmax()
    df = df[df["contract_date"] == front].copy()
    df["dt"] = pd.to_datetime(df["date"])
    df = df.sort_values("dt").reset_index(drop=True)
    return df[["dt", "price", "volume"]]


def build_equal_volume_buckets(ticks: pd.DataFrame, n_buckets_target: int = 50) -> pd.DataFrame:
    total_vol = ticks["volume"].sum()
    bucket_size = total_vol / n_buckets_target
    buckets = []
    cur_vol, cur_open, last_price, last_t = 0.0, None, None, None
    for _, r in ticks.iterrows():
        if cur_open is None:
            cur_open = r["price"]
        cur_vol += r["volume"]
        last_price, last_t = r["price"], r["dt"]
        if cur_vol >= bucket_size:
            buckets.append({"end_t": last_t, "open": cur_open, "close": last_price, "volume": cur_vol})
            cur_vol, cur_open = 0.0, None
    return pd.DataFrame(buckets)


def bvc_toxicity_series(buckets: pd.DataFrame, trailing_buckets: int = 30) -> pd.DataFrame:
    """Causal BVC/VPIN-style score per bucket. Buy/sell split classified via
    NORMAL CDF of the standardized within-bucket price change (VPIN paper
    uses a Student-t CDF; using normal here since bucket count is small and
    Spearman rank is what we care about -- noted per task instructions)."""
    buckets = buckets.copy()
    buckets["dP"] = buckets["close"].diff()
    scores = [np.nan] * len(buckets)
    for i in range(len(buckets)):
        lo = max(0, i - trailing_buckets + 1)
        window = buckets.iloc[lo : i + 1]
        dP_hist = window["dP"].dropna()
        if len(dP_hist) < 5:  # warm-up floor, causal (only past/current data)
            continue
        sigma = dP_hist.std(ddof=0)
        if sigma == 0 or np.isnan(sigma):
            continue
        z = window["dP"] / sigma
        buy_frac = norm.cdf(z)
        imbalance = np.abs(2 * buy_frac - 1) * window["volume"]
        scores[i] = float(imbalance.sum() / window["volume"].sum())
    buckets["toxicity"] = scores
    return buckets


def toxicity_at(buckets: pd.DataFrame, ts: pd.Timestamp) -> float | None:
    """Latest bucket whose end_t <= ts (causal -- no peeking at future ticks)."""
    prior = buckets[buckets["end_t"] <= ts]
    if prior.empty:
        return None
    val = prior.iloc[-1]["toxicity"]
    return None if pd.isna(val) else float(val)


def main() -> None:
    os.environ["ORDER_TMF_CHANNEL_NIGHT_USES_DAY_RECIPE"] = "1"
    from tmf_walkforward_harness import run_batch
    from order.tmf_channel_pv16_book import specialized_cell_book

    result = run_batch([DAY], session_pv_book=specialized_cell_book(), label="bvc-mae")
    trades = result["trades"]

    ticks = load_ticks(DAY)
    buckets = build_equal_volume_buckets(ticks, n_buckets_target=50)
    buckets = bvc_toxicity_series(buckets, trailing_buckets=30)

    pairs = []
    for t in trades:
        et = pd.Timestamp(t["et"]).tz_localize(None)  # tick dt is tz-naive local time
        tox = toxicity_at(buckets, et)
        pairs.append({"et": t["et"], "toxicity": tox, "mae": t["mae"], "pnl": t["pnl"], "why": t["why"]})

    print(json.dumps({"n_trades": len(trades), "n_buckets": len(buckets), "pairs": pairs}, indent=2, default=str))

    valid = [p for p in pairs if p["toxicity"] is not None]
    if len(valid) >= 3:
        rho, pval = spearmanr([p["toxicity"] for p in valid], [p["mae"] for p in valid])
        print(f"\nSpearman rho={rho:.3f} p={pval:.3f} n={len(valid)}")
    else:
        print(f"\nn={len(valid)} valid pairs -- correlation not meaningful (need n>=3)")


if __name__ == "__main__":
    main()
