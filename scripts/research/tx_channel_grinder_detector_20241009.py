#!/usr/bin/env python3
"""Grinder Detector adaptation test — 2024-10-09 day session TX/TMF micro-futures.

Builds causal VWAP(trailing 20-30min) + RSI(14, 1m) + relvol(1m vs trailing 60m avg)
on real tick-derived 1-min bars. Flags "grind-down" bars and checks whether forward
returns are more negative than baseline, and whether it adds info beyond PV8
classify_pv's contract/dry buckets.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_tick_validation import load_front_month_ticks, resample_to_1min  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from tmf_channel.causal_engine import classify_pv, rvol_series  # noqa: E402

DAY = "2024-10-09"


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.fillna(50.0)


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    if ticks is None:
        print("no ticks for", DAY)
        return
    bars = resample_to_1min(ticks).reset_index(drop=True)
    n = len(bars)
    print(f"day={DAY} n_bars={n}")

    close = bars["Close"]
    vol = bars["Volume"]

    # causal rolling VWAP, 25-min trailing window (mid of 20-30)
    typical = (bars["High"] + bars["Low"] + bars["Close"]) / 3.0
    pv = typical * vol
    vwap_win = 25
    vwap = pv.rolling(vwap_win, min_periods=5).sum() / vol.rolling(vwap_win, min_periods=5).sum()

    rsi = rsi_wilder(close, 14)

    relvol = vol / vol.rolling(60, min_periods=10).mean()

    below_vwap = (close < vwap).astype(int)
    below_count_15 = below_vwap.rolling(15, min_periods=15).sum()

    grind_down = (
        (below_count_15 >= 10)
        & (rsi >= 30) & (rsi <= 55)
        & (relvol < 1.5)
        & vwap.notna() & relvol.notna()
    )

    O = bars["Open"].tolist(); C = close.tolist(); V = vol.tolist()
    rv_pv8 = rvol_series(V)
    pv8_labels = []
    for t in range(n):
        lab, _ = classify_pv(C, O, rv_pv8, t)
        pv8_labels.append(lab)
    bars["pv8"] = pv8_labels
    bars["grind_down"] = grind_down.fillna(False)

    def fwd_ret(k: int) -> pd.Series:
        return (close.shift(-k) - close) / close * 10000.0  # bps (index pts proxy)

    fwd5 = fwd_ret(5)
    fwd10 = fwd_ret(10)
    fwd15 = fwd_ret(15)

    flagged = bars["grind_down"]
    n_events = int(flagged.sum())
    print(f"n_grind_down_events={n_events}")

    def stats(mask, label):
        f5 = fwd5[mask].dropna()
        f10 = fwd10[mask].dropna()
        f15 = fwd15[mask].dropna()
        print(f"{label}: n={mask.sum()} "
              f"fwd5 mean={f5.mean():.3f} hit%={ (f5<0).mean()*100:.1f} "
              f"fwd10 mean={f10.mean():.3f} hit%={(f10<0).mean()*100:.1f} "
              f"fwd15 mean={f15.mean():.3f} hit%={(f15<0).mean()*100:.1f}")
        return dict(fwd5=f5.mean(), fwd10=f10.mean(), fwd15=f15.mean(),
                     hit5=(f5 < 0).mean(), hit10=(f10 < 0).mean(), hit15=(f15 < 0).mean())

    grind_stats = stats(flagged, "GRIND-DOWN flagged")
    baseline_mask = pd.Series(True, index=bars.index)
    baseline_stats = stats(baseline_mask, "ALL bars (random baseline)")

    nonflagged_mask = ~flagged
    stats(nonflagged_mask, "NON-flagged bars")

    # PV8 cross-check
    pv8_dry_contract = bars["pv8"].isin(["dry", "contract"])
    n_pv8_dc = int(pv8_dry_contract.sum())
    overlap = flagged & pv8_dry_contract
    n_overlap = int(overlap.sum())
    overlap_pct = (n_overlap / n_pv8_dc * 100.0) if n_pv8_dc else float("nan")
    print(f"\nPV8 dry/contract bars: n={n_pv8_dc}, grind-down overlap n={n_overlap} ({overlap_pct:.1f}%)")

    dc_and_grind = pv8_dry_contract & flagged
    dc_not_grind = pv8_dry_contract & (~flagged)
    s_dc_grind = stats(dc_and_grind, "PV8 dry/contract AND grind-down")
    s_dc_nogrind = stats(dc_not_grind, "PV8 dry/contract, NOT grind-down")

    print("\nSUMMARY")
    print(f"grind_flagged fwd5={grind_stats['fwd5']:.3f} vs baseline fwd5={baseline_stats['fwd5']:.3f}")
    print(f"pv8_dry_contract overlap_pct={overlap_pct:.1f}")
    print(f"dc+grind fwd5={s_dc_grind.get('fwd5')} vs dc-only fwd5={s_dc_nogrind.get('fwd5')}")


if __name__ == "__main__":
    main()
