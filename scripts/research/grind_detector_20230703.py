#!/usr/bin/env python3
"""Grinder Detector (VWAP + RSI + relvol) vs PV8 classify_pv, single day 2023-07-03.

Causal build on 1-min day-session bars derived from real tick data via
tx_channel_tick_validation.load_front_month_ticks / resample_to_1min.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_tick_validation import load_front_month_ticks, resample_to_1min  # noqa: E402
from tmf_channel.causal_engine import classify_pv, rvol_series  # noqa: E402

DAY = "2023-07-03"


def rsi14(close: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(close)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    deltas = np.diff(close)
    gains = np.clip(deltas, 0, None)
    losses = np.clip(-deltas, 0, None)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / max(avg_loss, 1e-9))
    for t in range(period + 1, n):
        g = gains[t - 1]
        l = losses[t - 1]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        rs = avg_gain / max(avg_loss, 1e-9)
        out[t] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + rs)
    return out


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    assert ticks is not None, "no tick cache for day"
    bars = resample_to_1min(ticks)
    n = len(bars)
    C = bars["Close"].to_numpy(float)
    O = bars["Open"].to_numpy(float)
    V = bars["Volume"].to_numpy(float)

    # trailing 20-min VWAP (causal, using bar close as trade-price proxy)
    vwap_win = 20
    pv = C * V
    vwap = np.full(n, np.nan)
    for t in range(n):
        a = max(0, t - vwap_win + 1)
        vsum = V[a : t + 1].sum()
        vwap[t] = pv[a : t + 1].sum() / vsum if vsum > 0 else C[t]

    rsi = rsi14(C, 14)

    # relative volume: trailing 1-min vol vs trailing 60-min avg vol (excl current)
    relvol = np.full(n, np.nan)
    for t in range(1, n):
        a = max(0, t - 60)
        base = V[a:t].mean() if t > a else np.nan
        relvol[t] = V[t] / base if base and base > 0 else np.nan

    below_vwap = C < vwap
    grind = np.zeros(n, dtype=bool)
    for t in range(15, n):
        cnt_below = below_vwap[t - 14 : t + 1].sum()
        if (
            cnt_below >= 10
            and not np.isnan(rsi[t])
            and 30 <= rsi[t] <= 55
            and not np.isnan(relvol[t])
            and relvol[t] < 1.5
        ):
            grind[t] = True

    # PV8 classify_pv
    rvol = rvol_series(V.tolist())
    pv8 = [classify_pv(C, O, rvol, t, look=5)[0] for t in range(n)]

    def fwd_ret(t: int, k: int) -> float | None:
        if t + k >= n:
            return None
        return float(C[t + k] - C[t])

    def summarize(mask: np.ndarray, k: int) -> tuple[int, float, float]:
        rets = [fwd_ret(t, k) for t in range(n) if mask[t]]
        rets = [r for r in rets if r is not None]
        if not rets:
            return 0, float("nan"), float("nan")
        arr = np.array(rets)
        return len(arr), float(arr.mean()), float((arr < 0).mean())

    print(f"day={DAY} n_bars={n} n_grind_events={int(grind.sum())}")
    for k in (5, 10, 15):
        ng, mg, hg = summarize(grind, k)
        nb, mb, hb = summarize(np.ones(n, dtype=bool), k)  # baseline: all bars
        print(f"k={k:2d}min  grind: n={ng:4d} mean_fwd={mg:8.2f} hit_neg={hg:.3f} | "
              f"baseline(all-bars): n={nb:4d} mean_fwd={mb:8.2f} hit_neg={hb:.3f}")

    pv8_arr = np.array(pv8)
    contract_dry = np.isin(pv8_arr, ["contract", "dry"])
    overlap = grind & contract_dry
    n_cd = int(contract_dry.sum())
    n_overlap = int(overlap.sum())
    pct = n_overlap / n_cd * 100 if n_cd else float("nan")
    print(f"\nPV8 contract/dry bars: {n_cd}; also grind-flagged: {n_overlap} ({pct:.1f}%)")

    cd_not_grind = contract_dry & ~grind
    for k in (5, 10, 15):
        no, mo, ho = summarize(overlap, k)
        nn, mn, hn = summarize(cd_not_grind, k)
        print(f"k={k:2d}min  contract/dry+grind: n={no:4d} mean_fwd={mo:8.2f} hit_neg={ho:.3f} | "
              f"contract/dry only: n={nn:4d} mean_fwd={mn:8.2f} hit_neg={hn:.3f}")


if __name__ == "__main__":
    main()
