#!/usr/bin/env python3
"""micro-VCP (coil-then-volume-spike) hypothesis test, single-day tick check, 2023-07-03.

Construction (exactly as specified):
 (a) 3-min trailing price slope/direction (causal) -> trend context
 (b) coil: trailing-3-real-second volume < 50% of trailing-60s per-second avg volume
 (c) spike: the 1 real second where volume > 3x the trailing-60s baseline, firing
     immediately after a coil was true
 event = coil (at t-1s) then spike (at t)
 forward return over next 1/3/5 min checked for sign match with 3-min trend direction
 vs random non-event baseline of same size
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tx_channel_tick_validation import load_front_month_ticks, DAY_SESSION_START, DAY_SESSION_END  # noqa: E402

DAY = "2023-07-03"


def build_second_bars(ticks: pd.DataFrame) -> pd.DataFrame:
    t = ticks.set_index("dt").between_time(DAY_SESSION_START, DAY_SESSION_END)
    price = t["price"].resample("1s").last().ffill()
    vol = t["volume"].resample("1s").sum()
    out = pd.DataFrame({"price": price, "volume": vol})
    return out


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    if ticks is None or ticks.empty:
        print(f"NO TICK DATA for {DAY}")
        return
    sb = build_second_bars(ticks)
    n = len(sb)
    price = sb["price"].to_numpy()
    vol = sb["volume"].to_numpy()

    # (a) 3-min trailing trend: slope of price over trailing 180s, causal (uses price up to t)
    trend_dir = np.full(n, np.nan)
    for i in range(180, n):
        p0 = price[i - 180]
        p1 = price[i]
        trend_dir[i] = np.sign(p1 - p0)

    # (b) trailing-3s volume vs trailing-60s baseline (causal, excludes current second's own
    # contribution ambiguity by using [t-3,t) and [t-60,t) windows ending at t-1)
    csum = np.concatenate([[0.0], np.cumsum(vol)])
    def window_sum(end_excl, length):
        start = end_excl - length
        if start < 0:
            return np.nan
        return csum[end_excl] - csum[start]

    coil = np.zeros(n, dtype=bool)
    baseline_per_sec = np.full(n, np.nan)
    for i in range(60, n):
        base_sum = window_sum(i, 60)  # seconds [i-60, i)
        base_avg_per_sec = base_sum / 60.0
        trail3_sum = window_sum(i, 3)  # seconds [i-3, i)
        baseline_per_sec[i] = base_avg_per_sec
        if base_avg_per_sec > 0:
            coil[i] = (trail3_sum / 3.0) < 0.5 * base_avg_per_sec

    # (c) spike: second i has volume > 3x baseline_per_sec(i), and coil was true at i-1
    events = []
    for i in range(61, n):
        if np.isnan(baseline_per_sec[i]) or baseline_per_sec[i] <= 0:
            continue
        if vol[i] > 3.0 * baseline_per_sec[i] and coil[i - 1] and not np.isnan(trend_dir[i]):
            events.append(i)

    horizons = {"1min": 60, "3min": 180, "5min": 300}

    def fwd_stats(idx_list):
        hit = {h: [] for h in horizons}
        mag = {h: [] for h in horizons}
        for i in idx_list:
            td = trend_dir[i]
            if td == 0 or np.isnan(td):
                continue
            for h, secs in horizons.items():
                j = i + secs
                if j >= n:
                    continue
                r = price[j] - price[i]
                hit[h].append(1 if np.sign(r) == td else 0)
                mag[h].append(abs(r))
        return hit, mag

    ev_hit, ev_mag = fwd_stats(events)

    rng = np.random.default_rng(20230703)
    valid_base_idx = [i for i in range(61, n - 300) if not np.isnan(trend_dir[i])]
    if len(events) > 0 and len(valid_base_idx) > 0:
        base_sample = rng.choice(valid_base_idx, size=min(len(events) * 5, len(valid_base_idx)), replace=False)
    else:
        base_sample = []
    base_hit, base_mag = fwd_stats(base_sample)

    print(f"day={DAY}  n_second_bars={n}  n_coil+spike_events={len(events)}")
    for h in horizons:
        eh = ev_hit[h]
        bh = base_hit[h]
        em = ev_mag[h]
        bm = base_mag[h]
        print(
            f"  {h}: event_hit_rate={np.mean(eh) if eh else float('nan'):.3f} (n={len(eh)}) "
            f"baseline_hit_rate={np.mean(bh) if bh else float('nan'):.3f} (n={len(bh)}) "
            f"event_avg|ret|={np.mean(em) if em else float('nan'):.3f} "
            f"baseline_avg|ret|={np.mean(bm) if bm else float('nan'):.3f}"
        )

    if events:
        print("event second-indices (session-relative):", events[:20], "..." if len(events) > 20 else "")


if __name__ == "__main__":
    main()
