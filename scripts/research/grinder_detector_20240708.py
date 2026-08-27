#!/usr/bin/env python3
"""Grinder Detector (VWAP+RSI+relvol) causal check for 2024-07-08 day session,
plus PV8 (classify_pv, causal_engine.py) cross-check.

Adapted from "Fat Tony's Grinder Detector": slow, low-vol, sustained trend hugging
VWAP, distinguished from flat chop by RSI in a warm-not-extreme band + unremarkable
relative volume. This tests whether it adds info PV8 (rvol-only regime buckets)
lacks -- PV8 has no directional-persistence check, so a "陰跌" grind and genuine
flat chop both land in contract/dry.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tx_channel_tick_validation import load_front_month_ticks, resample_to_1min  # noqa: E402

DAY = "2024-07-08"


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    if ticks is None:
        print(f"{DAY}: no tick data")
        return
    bars = resample_to_1min(ticks)
    if bars.empty:
        print(f"{DAY}: empty resample")
        return
    bars = bars.reset_index(drop=True)
    C = bars["Close"].to_numpy(float)
    V = bars["Volume"].to_numpy(float)
    n = len(bars)
    print(f"{DAY}: {n} day-session 1min bars ({bars['Datetime'].iloc[0]} .. {bars['Datetime'].iloc[-1]})")

    # --- rolling VWAP (trailing 25 min, causal: window ending at t inclusive) ---
    typical = (bars["High"] + bars["Low"] + bars["Close"]) / 3.0
    pv = typical * bars["Volume"]
    VWAP_WIN = 25
    vwap = pv.rolling(VWAP_WIN, min_periods=10).sum() / bars["Volume"].rolling(VWAP_WIN, min_periods=10).sum()
    below_vwap = (bars["Close"] < vwap).to_numpy()

    # --- RSI(14) on 1min closes ---
    rsi14 = rsi(bars["Close"], 14).to_numpy()

    # --- relative volume: trailing 1min vol vs trailing 60min avg (causal, excl current-implicit ok) ---
    trail60 = bars["Volume"].rolling(60, min_periods=20).mean()
    relvol = (bars["Volume"] / trail60).to_numpy()

    # --- grind-down flag ---
    PERSIST_WIN = 15
    PERSIST_MIN = 10
    persist_below = pd.Series(below_vwap).rolling(PERSIST_WIN, min_periods=PERSIST_WIN).sum().to_numpy()

    grind_down = np.zeros(n, dtype=bool)
    for t in range(n):
        if t < max(VWAP_WIN, PERSIST_WIN, 60):
            continue
        if np.isnan(vwap.iloc[t]) or np.isnan(rsi14[t]) or np.isnan(relvol[t]):
            continue
        if persist_below[t] < PERSIST_MIN:
            continue
        if not (30.0 <= rsi14[t] <= 55.0):
            continue
        if relvol[t] >= 1.5:
            continue
        grind_down[t] = True

    idx_grind = np.where(grind_down)[0]
    print(f"grind-down flagged bars: {len(idx_grind)} / {n} valid-window bars")

    def fwd_ret(t: int, k: int) -> float | None:
        if t + k >= n:
            return None
        return float(C[t + k] - C[t])

    for k, label in ((5, "5min"), (10, "10min"), (15, "15min")):
        vals = [fwd_ret(t, k) for t in idx_grind]
        vals = [v for v in vals if v is not None]
        if vals:
            arr = np.array(vals)
            hit = float((arr < 0).mean())
            print(f"grind-down fwd_ret {label}: n={len(arr)} mean={arr.mean():.2f}pt "
                  f"median={np.median(arr):.2f}pt hit(<0)={hit:.1%}")

    # baseline: all valid bars (random period), same forward horizons
    valid_t = [t for t in range(n) if t >= max(VWAP_WIN, PERSIST_WIN, 60)
               and not np.isnan(vwap.iloc[t]) and not np.isnan(rsi14[t]) and not np.isnan(relvol[t])]
    for k, label in ((5, "5min"), (10, "10min"), (15, "15min")):
        vals = [fwd_ret(t, k) for t in valid_t]
        vals = [v for v in vals if v is not None]
        if vals:
            arr = np.array(vals)
            hit = float((arr < 0).mean())
            print(f"BASELINE(all)  fwd_ret {label}: n={len(arr)} mean={arr.mean():.2f}pt "
                  f"median={np.median(arr):.2f}pt hit(<0)={hit:.1%}")

    # --- PV8 cross-check ---
    from tmf_channel.causal_engine import classify_pv, rvol_series

    rv = rvol_series(list(V))
    O = bars["Open"].to_numpy(float)
    pv8_labels = []
    for t in range(n):
        lab, _ = classify_pv(C, O, rv, t)
        pv8_labels.append(lab)
    pv8_labels = np.array(pv8_labels)

    contract_dry_mask = np.isin(pv8_labels, ["contract", "dry"])
    contract_dry_idx = np.where(contract_dry_mask)[0]
    print(f"\nPV8 contract/dry bars: {len(contract_dry_idx)} / {n}")

    overlap = np.intersect1d(contract_dry_idx, idx_grind)
    overlap_pct = len(overlap) / len(contract_dry_idx) if len(contract_dry_idx) else None
    print(f"overlap (contract/dry AND grind-down): {len(overlap)}  "
          f"({overlap_pct:.1%} of contract/dry)" if overlap_pct is not None else "overlap: n/a")

    contract_dry_not_grind = np.setdiff1d(contract_dry_idx, idx_grind)
    for k, label in ((5, "5min"), (10, "10min"), (15, "15min")):
        v_flag = [fwd_ret(t, k) for t in overlap]
        v_flag = [v for v in v_flag if v is not None]
        v_noflag = [fwd_ret(t, k) for t in contract_dry_not_grind]
        v_noflag = [v for v in v_noflag if v is not None]
        m_flag = np.mean(v_flag) if v_flag else float("nan")
        m_noflag = np.mean(v_noflag) if v_noflag else float("nan")
        print(f"PV8 contract/dry+grind-flagged fwd_ret {label}: n={len(v_flag)} mean={m_flag:.2f}pt  |  "
              f"contract/dry NOT flagged: n={len(v_noflag)} mean={m_noflag:.2f}pt")


if __name__ == "__main__":
    main()
