#!/usr/bin/env python3
"""Grinder Detector prototype (VWAP + RSI + relvol) vs PV8 classify_pv, single day 2023-09-28.

Day-session only, causal (all features use info through bar t only).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_tick_validation import load_front_month_ticks, resample_to_1min  # noqa: E402

DAY = "2023-09-28"


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    out[avg_loss == 0.0] = 100.0
    return out


def rolling_vwap(df: pd.DataFrame, win: int) -> pd.Series:
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    pv = tp * df["Volume"]
    num = pv.rolling(win, min_periods=win).sum()
    den = df["Volume"].rolling(win, min_periods=win).sum().replace(0.0, np.nan)
    return num / den


def classify_pv_series(close: np.ndarray, vol: np.ndarray, win: int = 20, look: int = 5):
    """Reimplementation of causal_engine.classify_pv over full arrays (rv = V[t]/trailing median)."""
    import statistics as st

    n = len(close)
    out = [None] * n
    EXPAND, CONTRACT, CLIMAX, DRY = 1.50, 0.70, 2.50, 0.45
    for t in range(n):
        a = max(0, t - win + 1)
        med = st.median(vol[a : t + 1]) or 1.0
        rv = vol[t] / med
        if t < 1:
            out[t] = "na"
            continue
        aa = max(0, t - look)
        impulse = close[t] - close[aa]
        up = impulse > 0
        if rv >= CLIMAX:
            out[t] = "climax_up" if up else "climax_dn"
        elif rv >= EXPAND:
            out[t] = "expand_up" if up else "expand_dn"
        elif rv <= DRY:
            out[t] = "dry"
        elif rv <= CONTRACT:
            out[t] = "contract"
        else:
            hh = close[t] >= max(close[aa : t + 1]) - 1e-9
            if hh and rv < 1.0 and impulse > 0:
                out[t] = "div_hh_weak_vol"
            else:
                out[t] = "normal"
    return out


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    if ticks is None or ticks.empty:
        print(f"no ticks for {DAY}")
        return
    bars = resample_to_1min(ticks)
    bars = bars.reset_index(drop=True)
    n = len(bars)
    print(f"day={DAY} n_1min_bars={n}")

    close = bars["Close"].to_numpy(float)
    vol = bars["Volume"].to_numpy(float)

    vwap20 = rolling_vwap(bars, 20)
    vwap30 = rolling_vwap(bars, 30)
    rsi14 = rsi(bars["Close"], 14)
    relvol = bars["Volume"] / bars["Volume"].rolling(60, min_periods=10).mean()

    below20 = (bars["Close"] < vwap20).astype(int)
    persist20 = below20.rolling(15, min_periods=15).sum()

    grind_down = (
        (persist20 >= 10)
        & rsi14.between(30, 55)
        & (relvol < 1.5)
        & vwap20.notna()
    )

    pv8 = classify_pv_series(close, vol)
    bars["pv8"] = pv8
    bars["grind_down"] = grind_down.fillna(False)
    bars["vwap20"] = vwap20
    bars["rsi14"] = rsi14
    bars["relvol"] = relvol

    def fwd_ret(k):
        return (bars["Close"].shift(-k) - bars["Close"]) / bars["Close"] * 100.0

    for k in (5, 10, 15):
        bars[f"fwd{k}"] = fwd_ret(k)

    n_flag = int(bars["grind_down"].sum())
    print(f"n_grind_down_flags={n_flag}")

    baseline = {}
    grind = {}
    for k in (5, 10, 15):
        col = f"fwd{k}"
        baseline[k] = bars[col].mean()
        gsub = bars.loc[bars["grind_down"], col]
        grind[k] = gsub.mean()
        hit = (gsub < 0).mean() if len(gsub) else float("nan")
        base_hit = (bars[col] < 0).mean()
        print(f"k={k}min  grind_mean_ret={grind[k]:.4f}%  baseline_mean_ret={baseline[k]:.4f}%  "
              f"grind_hitrate(neg)={hit:.3f}  baseline_hitrate(neg)={base_hit:.3f}  n={len(gsub)}")

    print("\n-- PV8 cross-check --")
    contract_dry = bars["pv8"].isin(["contract", "dry"])
    n_cd = int(contract_dry.sum())
    overlap = bars.loc[contract_dry, "grind_down"]
    n_overlap = int(overlap.sum())
    pct = (n_overlap / n_cd * 100.0) if n_cd else float("nan")
    print(f"n_contract_or_dry={n_cd}  overlap_with_grind_down={n_overlap}  pct={pct:.1f}%")

    for k in (5, 10, 15):
        col = f"fwd{k}"
        cd_flagged = bars.loc[contract_dry & bars["grind_down"], col]
        cd_unflagged = bars.loc[contract_dry & ~bars["grind_down"], col]
        print(f"k={k}min  contract/dry+grind_down mean_ret={cd_flagged.mean():.4f}% (n={len(cd_flagged)})  "
              f"contract/dry~grind_down mean_ret={cd_unflagged.mean():.4f}% (n={len(cd_unflagged)})")

    out_path = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime" / f"grinder_detector_{DAY}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bars.to_csv(out_path, index=False)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
