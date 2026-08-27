#!/usr/bin/env python3
"""Micro-VCP coil-then-spike probe, single-day tick-level check (2023-12-28).

Construction (exact, causal at every point in time):
  (a) 3-min trend context: sign of Close[t] - Close[t-3min], sampled off a
      1-second causal price series (last trade price carried forward).
  (b) coil: trailing-3-real-second summed volume < 0.5 * trailing-60s
      per-second average volume (i.e. < 0.5 * (60s_sum/60)*3... see code).
  (c) spike: the current 1-second volume > 3x the trailing-60s per-second
      average volume, firing on a second where the coil condition was true
      on the immediately preceding second (coil-then-spike, not simultaneous
      coil+spike in the same second, since volume dry-up must precede the
      burst).

For every fired event, forward return (in index points) over next 1/3/5
minutes is compared against the 3-min trend direction (continuation = same
sign). Baseline = same statistic computed at random non-event seconds
(matched count), day session only (08:45-13:45), since night session ticks
in this cache would require cross-midnight file concatenation (out of scope
here, disclosed as caveat per repo convention).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tx_channel_tick_validation import load_front_month_ticks, DAY_SESSION_START, DAY_SESSION_END  # noqa: E402

DAY = "2023-12-28"
COIL_RATIO = 0.5
SPIKE_MULT = 3.0
RNG_SEED = 42


def build_1s_series(ticks: pd.DataFrame) -> pd.DataFrame:
    t = ticks.set_index("dt").between_time(DAY_SESSION_START, DAY_SESSION_END)
    if t.empty:
        return pd.DataFrame()
    price = t["price"].resample("1s").last().ffill()
    vol = t["volume"].resample("1s").sum()
    out = pd.DataFrame({"price": price, "volume": vol})
    return out


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    if ticks is None or ticks.empty:
        print(f"NO TICK DATA for {DAY}")
        return
    df = build_1s_series(ticks)
    n = len(df)

    # (a) causal 3-min trend: compare current price to price 180s ago
    price = df["price"].to_numpy()
    trend_sign = np.full(n, np.nan)
    trend_sign[180:] = np.sign(price[180:] - price[:-180])

    # (b)/(c) causal rolling volume stats, no future leakage
    vol = df["volume"].to_numpy(dtype=float)
    vol_60s_avg = df["volume"].rolling(60, min_periods=60).mean().to_numpy()  # per-second avg
    vol_3s_sum = df["volume"].rolling(3, min_periods=3).sum().to_numpy()
    coil = vol_3s_sum < COIL_RATIO * (vol_60s_avg * 3.0)
    spike = vol > SPIKE_MULT * vol_60s_avg

    # coil-then-spike: coil true on second t-1, spike fires on second t
    coil_prev = np.roll(coil, 1)
    coil_prev[0] = False
    event = spike & coil_prev
    valid = ~np.isnan(trend_sign) & ~np.isnan(vol_60s_avg)
    event = event & valid

    event_idx = np.where(event)[0]
    horizons = {"1min": 60, "3min": 180, "5min": 300}

    def fwd_stats(idx_arr):
        rows = []
        for h_name, h_sec in horizons.items():
            rets, hits = [], []
            for i in idx_arr:
                j = i + h_sec
                if j >= n or np.isnan(price[i]) or np.isnan(price[j]):
                    continue
                r = price[j] - price[i]
                rets.append(r)
                td = trend_sign[i]
                if td == 0 or np.isnan(td):
                    continue
                hits.append(1 if np.sign(r) == td else 0)
            rows.append((h_name, len(rets), np.nanmean(rets) if rets else np.nan,
                         np.mean(hits) if hits else np.nan))
        return rows

    event_stats = fwd_stats(event_idx)

    rng = np.random.default_rng(RNG_SEED)
    candidate_baseline = np.where(valid)[0]
    baseline_idx = rng.choice(candidate_baseline, size=min(len(event_idx) * 20, len(candidate_baseline)),
                               replace=False) if len(event_idx) > 0 else candidate_baseline
    baseline_stats = fwd_stats(baseline_idx)

    print(f"Day: {DAY}")
    print(f"1s bars in day session: {n}")
    print(f"Coil+spike events fired: {len(event_idx)}")
    print(f"Event second timestamps (HH:MM:SS from index): {[df.index[i].strftime('%H:%M:%S') for i in event_idx][:20]}")
    print()
    print(f"{'horizon':8} {'n_evt':>6} {'evt_avg_ret':>12} {'evt_hitrate':>12} {'n_base':>7} {'base_avg_ret':>13} {'base_hitrate':>13}")
    for (h, ne, ar, hr), (_, nb, br, bhr) in zip(event_stats, baseline_stats):
        print(f"{h:8} {ne:6d} {ar:12.3f} {hr if not np.isnan(hr) else float('nan'):12.3f} "
              f"{nb:7d} {br:13.3f} {bhr if not np.isnan(bhr) else float('nan'):13.3f}")


if __name__ == "__main__":
    main()
