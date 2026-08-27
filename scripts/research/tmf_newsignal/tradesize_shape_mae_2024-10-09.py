#!/usr/bin/env python3
"""Trade-SIZE-distribution SHAPE (not direction, not net imbalance) vs MAE
magnitude, single-day probe for 2024-10-09.

Detective angle (2026-08-13): Wyckoff effort/result divergence was already
tested and killed (day/night sign flip, struct_break exits only);
large-trade-net-volume-ratio was only tested directionally in a narrow
08:45-09:00 window. Neither touched the SHAPE of the trade-size
distribution itself. This probe asks: right before entry, is the tape's
size distribution unusually "spiky" (a few outsized prints dominating
volume) or unusually "flat" (many similar-sized prints) -- and does that
predict how far the trade goes against us (MAE) after entry?

Pipeline:
  1. Load raw ticks (price+volume+timestamp, no bid/ask) via
     tx_channel_tick_validation.load_front_month_ticks('2024-10-09').
  2. For each trade's entry time (et), take the trailing 5-minute window of
     ticks strictly BEFORE et (causal, no lookahead) and compute 3 trade-
     size-distribution-SHAPE features on that window's per-tick `volume`
     values (each tick IS one trade print here -- no separate trade-size
     field exists in this dataset, so tick volume is used as trade size,
     consistent with every other script in this repo that treats tick rows
     as prints):
       a) Hill tail-index estimate alpha_hat = 1 / gamma_hat, gamma_hat =
          (1/k) * sum_{i=1..k} ln(X_(i)/X_(k+1)) over the k largest sizes
          (descending order stats), k = max(10, round(0.05*n)). CAVEAT
          (discovered while building this): TX tick `volume` is a small
          bounded integer (2,4,6,...,~20 lots in this window), NOT a
          continuous heavy-tailed quantity -- classic Hill assumes a
          continuous right tail, so with this much tie-heavy discreteness
          the estimate is noisy/degenerate (ties give ln(1)=0 terms,
          inflating alpha_hat). Reported as-is per instructions, with this
          caveat, NOT silently swapped for something prettier.
       b) top-1%-by-size share of total window volume (ceil(1%*n) largest
          prints' summed volume / total window volume).
       c) Herfindahl index of trade sizes: sum((size_i / total_size)^2)
          over ALL prints in the window (volume-share HHI, not count-share).
  3. Run run_batch(['2024-10-09']) with the live baseline recipe
     (specialized_cell_book(), ORDER_TMF_CHANNEL_NIGHT_USES_DAY_RECIPE=1)
     to get real trades with mae (main hang_anchor entry, untested slice).
  4. Report each trade's (alpha_hat, top1pct_share, herfindahl, mae) and,
     only if n_trades >= 4, Spearman correlation of each feature vs mae.
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

import numpy as np
import pandas as pd

from tx_channel_tick_validation import load_front_month_ticks  # noqa: E402

DAY = "2024-10-09"
TRAIL_MIN = 5


def hill_alpha(sizes: np.ndarray) -> float | None:
    """Hill (1975) tail-index estimate. Returns alpha_hat = 1/gamma_hat, or
    None if n too small / degenerate (X_(k+1) <= 0)."""
    n = len(sizes)
    if n < 30:
        return None
    k = max(10, round(0.05 * n))
    k = min(k, n - 1)
    order = np.sort(sizes)[::-1]  # descending
    x_k1 = order[k]
    if x_k1 <= 0:
        return None
    top_k = order[:k]
    with np.errstate(divide="ignore"):
        logs = np.log(top_k / x_k1)
    gamma_hat = float(np.mean(logs))
    if gamma_hat <= 0:
        return None
    return 1.0 / gamma_hat


def top1pct_share(sizes: np.ndarray) -> float:
    n = len(sizes)
    k = max(1, math.ceil(0.01 * n))
    order = np.sort(sizes)[::-1]
    total = order.sum()
    if total <= 0:
        return float("nan")
    return float(order[:k].sum() / total)


def herfindahl(sizes: np.ndarray) -> float:
    total = sizes.sum()
    if total <= 0:
        return float("nan")
    shares = sizes / total
    return float(np.sum(shares ** 2))


def window_features(ticks: pd.Series, et: pd.Timestamp) -> dict:
    """ticks: Series of volume indexed by dt, sorted. Trailing TRAIL_MIN
    minutes strictly before et (causal)."""
    lo = et - pd.Timedelta(minutes=TRAIL_MIN)
    w = ticks.loc[lo:et]
    w = w[w.index < et]
    sizes = w.to_numpy(dtype=float)
    n = len(sizes)
    return {
        "n_ticks": n,
        "hill_alpha": hill_alpha(sizes) if n > 0 else None,
        "top1pct_share": top1pct_share(sizes) if n > 0 else None,
        "herfindahl": herfindahl(sizes) if n > 0 else None,
    }


def main() -> None:
    ticks_df = load_front_month_ticks(DAY)
    assert ticks_df is not None and not ticks_df.empty, "no ticks loaded"
    vol = ticks_df.set_index("dt")["volume"].sort_index()
    print(f"n_ticks={len(vol)} total_volume={int(vol.sum())}", file=sys.stderr)

    os.environ["ORDER_TMF_CHANNEL_NIGHT_USES_DAY_RECIPE"] = "1"
    from order.tmf_channel_pv16_book import specialized_cell_book
    from tmf_walkforward_harness import run_batch

    book = specialized_cell_book()
    result = run_batch([DAY], session_pv_book=book, label="tradesize-shape-mae-2024-10-09")
    trades = result["trades"]
    print(f"n_trades={len(trades)}", file=sys.stderr)

    pairs = []
    for tr in trades:
        et = pd.Timestamp(tr["et"]).tz_localize(None)
        feats = window_features(vol, et)
        pairs.append(dict(
            et=tr["et"], side=tr["s"], mae=tr["mae"], pnl=tr["pnl"], why=tr["why"],
            n_ticks_in_window=feats["n_ticks"],
            hill_alpha=feats["hill_alpha"],
            top1pct_share=feats["top1pct_share"],
            herfindahl=feats["herfindahl"],
        ))

    print("\n--- pairs ---")
    for p in pairs:
        print(p)

    n = len(pairs)
    if n >= 4:
        mae_vals = pd.Series([p["mae"] for p in pairs])
        for feat in ("hill_alpha", "top1pct_share", "herfindahl"):
            vals = pd.Series([p[feat] for p in pairs])
            valid_mask = vals.notna()
            if valid_mask.sum() >= 4:
                rho = vals[valid_mask].corr(mae_vals[valid_mask], method="spearman")
                print(f"\nSpearman({feat}, mae) n={int(valid_mask.sum())}: {rho}")
            else:
                print(f"\n{feat}: only {int(valid_mask.sum())} valid values -- correlation not computed")
    else:
        print(f"\nn_trades={n} < 4 -- correlation not meaningful per task instructions, raw pairs only (above)")


if __name__ == "__main__":
    main()
