#!/usr/bin/env python3
"""micro-VCP coil-then-spike hypothesis test, single day 2024-10-09, TX front-month tick data.

Construction (per spec, day session 08:45-13:45 only, causal at every step):
  (a) 3-min trend context: trend_dir(t) = sign(price[t] - price[t-180s])
  (b) coil: trailing-3s volume < 50% of a trailing-60s per-second baseline
      (baseline computed over [t-60,t-1], excluding the candidate spike second itself)
  (c) spike: volume at second t > 3x that same 60s baseline, firing on a second where
      the coil condition was true immediately prior (seconds t-3..t-1)

For every coil+spike event, forward return at 1/3/5 min is compared in sign to trend_dir
("continuation" if signs match). A baseline sample of random non-event seconds (same day,
same causal windows) gets the identical treatment for comparison.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tx_channel_tick_validation import load_front_month_ticks, DAY_SESSION_START, DAY_SESSION_END  # noqa: E402

DAY = "2024-10-09"
COIL_RATIO = 0.5     # trailing-3s vol < 50% of expected 3s vol from 60s baseline
SPIKE_MULT = 3.0     # spike second vol > 3x 60s baseline
TREND_WINDOW_S = 180
HORIZONS_S = {"1min": 60, "3min": 180, "5min": 300}
RNG_SEED = 20241009
N_BASELINE_SAMPLES = 2000


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    if ticks is None or ticks.empty:
        print("NO TICK DATA")
        return

    ticks = ticks.set_index("dt").between_time(DAY_SESSION_START, DAY_SESSION_END)
    if ticks.empty:
        print("NO DAY-SESSION TICKS")
        return

    # per-second causal grid: last trade price in the second (ffill), summed volume
    price_1s = ticks["price"].resample("1s").last().ffill()
    vol_1s = ticks["volume"].resample("1s").sum().fillna(0)
    idx = price_1s.index
    n = len(idx)
    price = price_1s.to_numpy()
    vol = vol_1s.to_numpy()

    # (a) trailing 3-min trend
    trend_dir = np.full(n, np.nan)
    trend_dir[TREND_WINDOW_S:] = np.sign(price[TREND_WINDOW_S:] - price[:-TREND_WINDOW_S])

    # rolling sums (causal, inclusive of current second)
    vol_series = pd.Series(vol)
    vol_3s = vol_series.rolling(3).sum().to_numpy()          # [t-2..t]
    vol_60s = vol_series.rolling(60).sum().to_numpy()        # [t-59..t]

    max_h = max(HORIZONS_S.values())
    events = []
    for t in range(60, n - max_h):
        if np.isnan(trend_dir[t]) or trend_dir[t] == 0:
            continue
        # baseline over [t-60, t-1], excludes second t itself
        base_60 = vol_series.iloc[t - 60:t].sum()
        base_persec = base_60 / 60.0
        if base_persec <= 0:
            continue
        # coil: trailing 3s ending at t-1, i.e. seconds [t-3, t-1]
        coil_3s = vol_series.iloc[t - 3:t].sum()
        is_coil = coil_3s < COIL_RATIO * (base_persec * 3)
        is_spike = vol[t] > SPIKE_MULT * base_persec
        if is_coil and is_spike:
            events.append(t)

    def fwd_returns(t: int) -> dict[str, float]:
        return {h: price[t + s] - price[t] for h, s in HORIZONS_S.items()}

    def eval_point(t: int) -> dict:
        rets = fwd_returns(t)
        d = trend_dir[t]
        out = {"t": t, "trend_dir": d}
        for h, r in rets.items():
            out[f"ret_{h}"] = r
            out[f"hit_{h}"] = (np.sign(r) == d) if r != 0 else False
        return out

    event_rows = [eval_point(t) for t in events]

    rng = np.random.default_rng(RNG_SEED)
    valid_range = np.arange(180, n - max_h)
    valid_range = valid_range[~np.isnan(trend_dir[valid_range])]
    valid_range = valid_range[trend_dir[valid_range] != 0]
    sample_t = rng.choice(valid_range, size=min(N_BASELINE_SAMPLES, len(valid_range)), replace=False)
    baseline_rows = [eval_point(int(t)) for t in sample_t]

    n_events = len(event_rows)
    print(f"Day: {DAY}")
    print(f"Session seconds analyzed: {n} (08:45-13:45)")
    print(f"Coil+spike events fired: {n_events}")
    print(f"Baseline sample size: {len(baseline_rows)}")

    if n_events == 0:
        print("n=0 events -> no signal-check possible this day.")
        return

    ev_df = pd.DataFrame(event_rows)
    bl_df = pd.DataFrame(baseline_rows)

    for h in HORIZONS_S:
        ev_hit = ev_df[f"hit_{h}"].mean()
        bl_hit = bl_df[f"hit_{h}"].mean()
        ev_mag = ev_df[f"ret_{h}"].abs().mean()
        bl_mag = bl_df[f"ret_{h}"].abs().mean()
        print(f"[{h}] event hit-rate={ev_hit:.3f} (n={n_events}) vs baseline hit-rate={bl_hit:.3f} (n={len(bl_df)})"
              f" | event |ret| avg={ev_mag:.2f} pt vs baseline |ret| avg={bl_mag:.2f} pt")

    if n_events < 5:
        print("CAVEAT: n<5 events -> no real signal-check possible from this single day.")

    print("\nEvent timestamps (HH:MM:SS):")
    for t in events:
        print(" ", idx[t].strftime("%H:%M:%S"), "trend_dir=", trend_dir[t])


if __name__ == "__main__":
    main()
