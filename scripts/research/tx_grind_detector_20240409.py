#!/usr/bin/env python3
"""Grinder-Detector (VWAP + RSI + relvol) test vs PV8 classify_pv, single day 2024-04-09."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tx_channel_tick_validation import load_front_month_ticks, resample_to_1min  # noqa: E402
from tmf_channel.causal_engine import rvol_series, classify_pv  # noqa: E402

DAY = "2024-04-09"


def rsi_wilder(close: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(close)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = gain[:period].mean()
    avg_loss = loss[:period].mean()
    rs = avg_gain / avg_loss if avg_loss > 0 else np.inf
    out[period] = 100 - 100 / (1 + rs)
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else np.inf
        out[i + 1] = 100 - 100 / (1 + rs)
    return out


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    if ticks is None:
        print("NO TICK DATA")
        return
    bars = resample_to_1min(ticks)
    n = len(bars)
    C = bars["Close"].to_numpy(float)
    H = bars["High"].to_numpy(float)
    L = bars["Low"].to_numpy(float)
    O = bars["Open"].to_numpy(float)
    V = bars["Volume"].to_numpy(float)
    typ = (H + L + C) / 3.0

    # causal trailing VWAP, 20-bar window (ending at t inclusive)
    VWAP_WIN = 20
    vwap = np.full(n, np.nan)
    for t in range(n):
        a = max(0, t - VWAP_WIN + 1)
        num = (typ[a : t + 1] * V[a : t + 1]).sum()
        den = V[a : t + 1].sum()
        vwap[t] = num / den if den > 0 else np.nan

    rsi = rsi_wilder(C, 14)

    RELVOL_WIN = 60
    relvol = np.full(n, np.nan)
    for t in range(n):
        a = max(0, t - RELVOL_WIN)
        prior = V[a:t]
        if len(prior) >= 10:
            m = prior.mean()
            relvol[t] = V[t] / m if m > 0 else np.nan

    below_vwap = C < vwap

    LOOKBACK = 15
    MIN_BELOW = 10
    grind_down = np.zeros(n, dtype=bool)
    for t in range(n):
        a = max(0, t - LOOKBACK + 1)
        window = below_vwap[a : t + 1]
        if len(window) < LOOKBACK:
            continue
        n_below = window.sum()
        if (
            n_below >= MIN_BELOW
            and not np.isnan(rsi[t])
            and 30.0 <= rsi[t] <= 55.0
            and not np.isnan(relvol[t])
            and relvol[t] < 1.5
        ):
            grind_down[t] = True

    def fwd_ret(t: int, k: int) -> float | None:
        if t + k >= n:
            return None
        return float(C[t + k] - C[t])

    def summarize(idx: np.ndarray, label: str) -> dict:
        out = {"label": label, "n": int(len(idx))}
        for k in (5, 10, 15):
            rets = [fwd_ret(t, k) for t in idx]
            rets = [r for r in rets if r is not None]
            if rets:
                arr = np.array(rets)
                out[f"fwd{k}_mean"] = float(arr.mean())
                out[f"fwd{k}_hitrate_neg"] = float((arr < 0).mean())
                out[f"fwd{k}_n"] = len(arr)
        return out

    grind_idx = np.where(grind_down)[0]
    all_idx = np.arange(n)

    print(f"=== {DAY}  n_bars={n} ===")
    print(f"n_grind_down_events = {len(grind_idx)}")
    print(summarize(grind_idx, "grind_down"))
    print(summarize(all_idx, "baseline_all_bars"))

    # PV8 classify
    rvol_pv8 = rvol_series(V.tolist())
    pv8_states = []
    for t in range(n):
        st, impulse = classify_pv(C.tolist(), O.tolist(), rvol_pv8, t)
        pv8_states.append(st)
    pv8_states = np.array(pv8_states)

    contract_dry_idx = np.where(np.isin(pv8_states, ["contract", "dry"]))[0]
    overlap = np.intersect1d(contract_dry_idx, grind_idx)
    overlap_pct = len(overlap) / len(contract_dry_idx) * 100 if len(contract_dry_idx) else None
    print(f"\nPV8 contract/dry bars = {len(contract_dry_idx)}; overlap with grind_down = {len(overlap)} "
          f"({overlap_pct:.1f}%)" if overlap_pct is not None else "no contract/dry bars")

    cd_flagged = overlap
    cd_unflagged = np.setdiff1d(contract_dry_idx, grind_idx)
    print(summarize(cd_flagged, "contract_dry_AND_grind"))
    print(summarize(cd_unflagged, "contract_dry_NOT_grind"))


if __name__ == "__main__":
    main()
