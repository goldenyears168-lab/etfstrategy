#!/usr/bin/env python3
"""Grinder Detector (VWAP + RSI + relvol grind-down) vs PV8 classify_pv, day 2023-12-28.

Ad-hoc research script for a one-off hypothesis test — not part of any pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tx_channel_tick_validation import load_front_month_ticks, resample_to_1min  # noqa: E402
from tmf_channel.causal_engine import classify_pv, rvol_series  # noqa: E402

DAY = "2023-12-28"


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    if ticks is None or ticks.empty:
        print("no ticks for", DAY)
        return
    bars = resample_to_1min(ticks)
    bars = bars.reset_index(drop=True)
    n = len(bars)
    print(f"day={DAY} n_1min_bars={n}")

    C = bars["Close"].to_numpy(float)
    O = bars["Open"].to_numpy(float)
    V = bars["Volume"].to_numpy(float)

    # --- causal rolling VWAP (trailing 20 min, typical price = close) ---
    VWAP_WIN = 20
    pv = C * V
    vwap = np.full(n, np.nan)
    for t in range(n):
        a = max(0, t - VWAP_WIN + 1)
        vsum = V[a : t + 1].sum()
        vwap[t] = pv[a : t + 1].sum() / vsum if vsum > 0 else C[t]

    # --- causal RSI(14) on 1-min closes ---
    RSI_N = 14
    delta = np.diff(C, prepend=C[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    rsi = np.full(n, np.nan)
    for t in range(n):
        a = max(0, t - RSI_N + 1)
        ag = gain[a : t + 1].mean()
        al = loss[a : t + 1].mean()
        rsi[t] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)

    # --- causal relative volume: trailing 1-min vol vs trailing 60-min avg (excl current) ---
    RVOL_WIN = 60
    relvol = np.full(n, np.nan)
    for t in range(n):
        a = max(0, t - RVOL_WIN)
        hist = V[a:t]  # strictly prior bars
        avg = hist.mean() if len(hist) > 0 else np.nan
        relvol[t] = V[t] / avg if avg and avg > 0 else np.nan

    # --- below-VWAP persistence: below VWAP for >=10 of last 15 bars ---
    below = (C < vwap).astype(int)
    persist_win = 15
    below_count = np.full(n, 0)
    for t in range(n):
        a = max(0, t - persist_win + 1)
        below_count[t] = below[a : t + 1].sum()

    grind_down = (
        (below_count >= 10)
        & (rsi >= 30) & (rsi <= 55)
        & (relvol < 1.5) & ~np.isnan(relvol)
    )

    # --- forward returns (points) ---
    def fwd_ret(k):
        out = np.full(n, np.nan)
        for t in range(n - k):
            out[t] = C[t + k] - C[t]
        return out

    fwd5, fwd10, fwd15 = fwd_ret(5), fwd_ret(10), fwd_ret(15)

    flagged_idx = np.where(grind_down)[0]
    flagged_idx = flagged_idx[flagged_idx < n - 15]  # need full fwd15 horizon
    all_idx = np.arange(n - 15)

    def stats(idx, arr):
        vals = arr[idx]
        vals = vals[~np.isnan(vals)]
        if len(vals) == 0:
            return dict(n=0, mean=None, hit_neg=None)
        return dict(n=len(vals), mean=float(vals.mean()),
                    hit_neg=float((vals < 0).mean()))

    print(f"\n== Grind-down flagged events: n={len(flagged_idx)} (of {len(all_idx)} eligible bars) ==")
    for k, arr in [(5, fwd5), (10, fwd10), (15, fwd15)]:
        s_fl = stats(flagged_idx, arr)
        s_all = stats(all_idx, arr)
        print(f"fwd{k}min: flagged mean={s_fl['mean']:.2f} pt hit_neg={s_fl['hit_neg']:.1%} n={s_fl['n']}"
              f"  | baseline(all bars) mean={s_all['mean']:.2f} pt hit_neg={s_all['hit_neg']:.1%} n={s_all['n']}")

    # --- PV8 classify_pv cross-check ---
    rvol_pv8 = rvol_series(list(V))
    pv8_labels = []
    for t in range(n):
        lab, _imp = classify_pv(list(C), list(O), rvol_pv8, t)
        pv8_labels.append(lab)
    pv8_labels = np.array(pv8_labels)

    contract_dry_mask = np.isin(pv8_labels, ["contract", "dry"])
    contract_dry_idx = np.where(contract_dry_mask)[0]
    contract_dry_idx = contract_dry_idx[contract_dry_idx < n - 15]

    overlap = grind_down[contract_dry_idx]
    n_cd = len(contract_dry_idx)
    n_cd_flagged = int(overlap.sum())
    pct = (n_cd_flagged / n_cd * 100) if n_cd else None
    print(f"\n== PV8 cross-check ==")
    print(f"contract/dry bars: n={n_cd}; also grind-down-flagged: {n_cd_flagged} ({pct:.1f}%)" if n_cd else "no contract/dry bars")

    cd_flagged_idx = contract_dry_idx[overlap]
    cd_unflagged_idx = contract_dry_idx[~overlap]
    for k, arr in [(5, fwd5), (10, fwd10), (15, fwd15)]:
        s_f = stats(cd_flagged_idx, arr)
        s_u = stats(cd_unflagged_idx, arr)
        print(f"fwd{k}min within contract/dry: grind-flagged mean={s_f['mean']} n={s_f['n']}"
              f"  | unflagged mean={s_u['mean']} n={s_u['n']}")


if __name__ == "__main__":
    main()
