"""Ad-hoc round 14: tick-level re-verification of the round 11-13 "hold until
reversion + stop-loss" backtest, following this repo's own established
practice (see tx_channel_tick_validation.py's docstring — bar-level
approximation is documented as the single most common false-edge source in
this codebase: one prior strategy's numbers shrank 42% under tick-level
confirmation; another needed 11 rounds / 200k+ combos before a version
survived tick reweighting).

The bug in round 11-13: touch/stop conditions were checked against 1-minute
bar CLOSES only. A price could touch the stop mid-minute and recover to close
inside the profit zone by minute-end — the bar-close check would never see
the stop, silently overstating performance (or understating it, direction
isn't obvious a priori — that's exactly why it must be checked, not assumed).

This version: signal (center/amp, computed from 1-min bars — the "30-min
channel" is inherently a bar-aggregate construct, that part is legitimate)
stays as before, but ENTRY and EXIT execution scan the real tick stream tick
by tick, so stop-vs-touch ordering within a minute is resolved correctly and
exit price is a real achievable tick print, not a bar close.

Uses load_front_month_ticks from tx_channel_tick_validation.py (the repo's
own validated front-month + spread-quote filter). Not wired into any
pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
import pandas as pd

from tx_channel_tick_validation import load_front_month_ticks
from tx_channel_amp_volume_interaction import load_all_day_bars, weighted_amp30
from tx_channel_amp_persistence_drivers import day_clustered
from tx_channel_slope_persistence_140d import rolling_slope_center

COST = 2.0
DAY_START = "08:45:00"
DAY_END = "13:45:00"


def backtest_day_tick_level(ticks: pd.DataFrame, bars: pd.DataFrame, center: np.ndarray, amp: np.ndarray, K: float, max_hold_min: int, stop_k: float | None):
    """ticks: columns dt, price, volume (day-session only). bars/center/amp:
    1-min signal computed exactly as before. Entries/exits execute against
    the real tick stream."""
    bar_times = bars["t"].tolist()  # minute-boundary timestamps, one per bar
    n_bars = len(bars)
    trades = []
    in_pos = False
    for i in range(29, n_bars):
        if np.isnan(amp[i]) or amp[i] <= 0 or np.isnan(center[i]):
            continue
        if in_pos:
            continue
        z = (center[i] - bars["c"].iat[i]) / amp[i]
        if abs(z) < K:
            continue
        direction = 1 if z > 0 else -1
        entry_center = center[i]
        entry_amp = amp[i]
        signal_time = bar_times[i]  # bar close time == signal confirmation instant
        max_hold_delta = pd.Timedelta(minutes=max_hold_min)
        day_end_ts = pd.Timestamp(f"{signal_time.date()} {DAY_END}")

        # entry: first tick strictly after the signal bar closes (avoid same-instant look-ahead)
        entry_ticks = ticks[ticks["dt"] > signal_time]
        if entry_ticks.empty:
            continue
        entry_row = entry_ticks.iloc[0]
        entry_price = float(entry_row["price"])
        entry_time = entry_row["dt"]

        deadline = min(entry_time + max_hold_delta, day_end_ts)
        window = ticks[(ticks["dt"] > entry_time) & (ticks["dt"] <= deadline)]
        if window.empty:
            # no more ticks before deadline -> forced flat at entry price (no better info available)
            trades.append(dict(net=-COST, hold_sec=0, exit_reason="no_ticks"))
            in_pos = False
            continue

        prices = window["price"].to_numpy()
        times = window["dt"].to_numpy()
        exit_price = None
        exit_reason = None
        exit_time = None
        for p, t_ in zip(prices, times):
            adverse = direction * (entry_price - p)
            stopped = stop_k is not None and adverse >= stop_k * entry_amp
            touched = (direction == 1 and p >= entry_center) or (direction == -1 and p <= entry_center)
            if stopped or touched:
                exit_price = p
                exit_reason = "stop" if stopped else "touch"
                exit_time = t_
                break
        if exit_price is None:
            exit_price = prices[-1]
            exit_time = times[-1]
            exit_reason = "eod" if pd.Timestamp(exit_time) >= day_end_ts - pd.Timedelta(minutes=1) else "timeout"

        net = direction * (exit_price - entry_price) - COST
        hold_sec = (pd.Timestamp(exit_time) - entry_time).total_seconds()
        trades.append(dict(net=net, hold_sec=hold_sec, exit_reason=exit_reason))
        in_pos = False
    return trades


def main():
    bars_by_day = load_all_day_bars()
    dates = sorted(bars_by_day)
    print(f"n days = {len(dates)}")

    K, max_hold_min, stop_k = 1.5, 250, 2.0  # a representative config from round 11-13
    all_by_day = {}
    n_missing = 0
    for date in dates:
        bars = bars_by_day[date]
        _, center = rolling_slope_center(bars)
        amp = weighted_amp30(bars)
        ticks = load_front_month_ticks(date)
        if ticks is None or ticks.empty:
            n_missing += 1
            continue
        ticks = ticks[(ticks["dt"].dt.strftime("%H:%M:%S") >= DAY_START) & (ticks["dt"].dt.strftime("%H:%M:%S") <= DAY_END)]
        if ticks.empty:
            n_missing += 1
            continue
        trades = backtest_day_tick_level(ticks, bars, center, amp, K, max_hold_min, stop_k)
        all_by_day[date] = trades
    print(f"days with usable ticks = {len(all_by_day)} (missing/empty = {n_missing})")

    day_means = [np.mean([tr["net"] for tr in trs]) for trs in all_by_day.values() if trs]
    m, t, p = day_clustered(day_means)
    all_trades = [tr for trs in all_by_day.values() for tr in trs]
    hit = np.mean([tr["net"] > 0 for tr in all_trades])
    reasons = {}
    for tr in all_trades:
        reasons[tr["exit_reason"]] = reasons.get(tr["exit_reason"], 0) + 1
    reason_str = " ".join(f"{k}={v/len(all_trades)*100:.1f}%" for k, v in sorted(reasons.items()))
    print(f"\nTICK-LEVEL: K={K} max_hold={max_hold_min}min stop_k={stop_k}")
    print(f"n_trades={len(all_trades)}  n_days={len(day_means)}  hit={hit*100:.1f}%  net/trade(day-clust)={m:.2f}  t={t:.2f}  p={p:.4f}")
    print(f"exit reasons: {reason_str}")
    print(f"median hold: {np.median([tr['hold_sec'] for tr in all_trades])/60:.1f} min")


if __name__ == "__main__":
    main()
