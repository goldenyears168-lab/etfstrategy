#!/usr/bin/env python3
"""Micro-VCP (coil-then-volume-spike) hypothesis test, single day 2024-07-08, tick resolution.

Construction (user-specified, exact):
  (a) 3-min trailing trend context: sign(price[t] - price[t-180s]), causal.
  (b) "coil": trailing-3-real-second summed volume < 50% of the trailing-60s
      per-second-average volume * 3 (i.e. below half the "expected" 3s volume
      at the recent normal rate). Causal, no future data.
  (c) "spike": the very next real second's volume > 3x the trailing-60s
      per-second-average volume, firing right after a second where the coil
      condition was true.

For each coil+spike event, forward return at 1/3/5 min is compared in sign
to the 3-min trend direction at event time (continuation = match). Baseline
= random non-event seconds, same local-trend-direction-vs-forward-return
match test, to get the chance-level hit rate on this exact day/tape.

Day session only (08:45-13:45), consistent with tx_channel_* convention.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tx_channel_tick_validation import load_front_month_ticks  # noqa: E402

DAY = "2024-07-08"
SESSION_START = "08:45:00"
SESSION_END = "13:45:00"

TREND_WIN_S = 180      # 3-min trailing trend context
COIL_WIN_S = 3          # trailing real seconds for coil volume
BASE_WIN_S = 60         # trailing baseline window
COIL_FRAC = 0.5          # coil: trailing3s vol < 50% of expected 3s vol at baseline rate
SPIKE_MULT = 3.0         # spike: 1s vol > 3x baseline per-second rate
HORIZONS = {"1min": 60, "3min": 180, "5min": 300}

RNG_SEED = 20240708


def build_second_series(ticks: pd.DataFrame) -> pd.DataFrame:
    ticks = ticks.set_index("dt").between_time(SESSION_START, SESSION_END)
    price = ticks["price"].resample("1s").last().ffill()
    vol = ticks["volume"].resample("1s").sum()
    df = pd.DataFrame({"price": price, "volume": vol})
    df = df.dropna(subset=["price"])
    return df.reset_index().rename(columns={"dt": "ts"})


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    if ticks is None or ticks.empty:
        print(f"NO TICK DATA for {DAY}")
        return
    df = build_second_series(ticks)
    n = len(df)
    price = df["price"].to_numpy(float)
    vol = df["volume"].to_numpy(float)

    trailing3s_vol = pd.Series(vol).rolling(COIL_WIN_S).sum().to_numpy()
    base60_persec = pd.Series(vol).rolling(BASE_WIN_S).mean().to_numpy()

    coil = trailing3s_vol < COIL_FRAC * (base60_persec * COIL_WIN_S)
    spike = vol > SPIKE_MULT * base60_persec

    trend_dir = np.full(n, np.nan)
    valid_trend = np.arange(n) >= TREND_WIN_S
    trend_dir[valid_trend] = np.sign(price[valid_trend] - price[np.arange(n)[valid_trend] - TREND_WIN_S])

    max_h = max(HORIZONS.values())
    valid_range = np.arange(n)
    has_history = valid_range >= max(TREND_WIN_S, BASE_WIN_S)
    has_future = valid_range < (n - max_h)

    event_idx = []
    for t in range(1, n):
        if not (has_history[t] and has_future[t]):
            continue
        if spike[t] and coil[t - 1] and not np.isnan(trend_dir[t]):
            event_idx.append(t)
    event_idx = np.array(event_idx, dtype=int)

    def eval_points(idxs: np.ndarray) -> dict:
        out = {}
        for label, h in HORIZONS.items():
            fwd_ret = price[idxs + h] - price[idxs]
            td = trend_dir[idxs]
            match = np.sign(fwd_ret) == td
            out[f"hit_rate_{label}"] = float(np.mean(match)) if len(idxs) else None
            out[f"avg_abs_fwd_ret_{label}"] = float(np.mean(np.abs(fwd_ret))) if len(idxs) else None
        return out

    n_events = int(len(event_idx))
    event_stats = eval_points(event_idx) if n_events else {}

    eligible = np.where(has_history & has_future & ~np.isnan(trend_dir))[0]
    eligible = eligible[eligible >= 1]
    rng = np.random.default_rng(RNG_SEED)
    baseline_n = max(n_events * 20, 200)
    baseline_n = min(baseline_n, len(eligible))
    baseline_idx = rng.choice(eligible, size=baseline_n, replace=False) if baseline_n else np.array([], dtype=int)
    baseline_stats = eval_points(baseline_idx) if baseline_n else {}

    print(f"day={DAY} session=day(08:45-13:45) seconds_in_series={n}")
    print(f"n_coil_spike_events={n_events}")
    if n_events:
        for label in HORIZONS:
            print(f"  {label}: event_hit_rate={event_stats[f'hit_rate_{label}']:.3f} "
                  f"baseline_hit_rate={baseline_stats.get(f'hit_rate_{label}'):.3f} "
                  f"event_avg|fwd_ret|={event_stats[f'avg_abs_fwd_ret_{label}']:.2f} "
                  f"baseline_avg|fwd_ret|={baseline_stats.get(f'avg_abs_fwd_ret_{label}'):.2f}")
    else:
        print("no events fired -- no signal-check possible")

    if n_events and n_events < 5:
        print("CAVEAT: n<5 events this day -- no real signal-check is possible from this single day alone.")


if __name__ == "__main__":
    main()
