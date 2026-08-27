#!/usr/bin/env python3
"""Micro-VCP (coil-then-volume-spike continuation) tick-level probe, single day 2023-09-28.

Construction (per spec):
  (a) 3-min trailing price slope/direction, causal.
  (b) coil: trailing-3-real-second traded volume < 50% of trailing-60s avg per-second volume.
  (c) spike: the 1 real second where volume >= 3x the trailing-60s avg per-second volume,
      firing right after a coil was just true.
  For each such event compute fwd return at 1/3/5 min and check sign match vs 3-min trend
  direction (continuation), vs a random non-event baseline.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tx_channel_tick_validation import load_front_month_ticks, DAY_SESSION_START, DAY_SESSION_END  # noqa: E402

DAY = "2023-09-28"


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    if ticks is None or ticks.empty:
        print(f"NO TICK DATA for {DAY}")
        return
    ticks = ticks.set_index("dt").between_time(DAY_SESSION_START, DAY_SESSION_END)
    ticks = ticks[~ticks.index.duplicated(keep="first")]

    # per-second volume series (real seconds, gaps = 0 volume)
    sec_vol = ticks["volume"].resample("1s").sum()
    sec_px = ticks["price"].resample("1s").last().ffill()

    # trailing baselines, causal (shift excludes current second where needed via closed windows)
    baseline60 = sec_vol.rolling("60s", min_periods=30).mean()
    trail3 = sec_vol.rolling("3s", min_periods=3).sum()
    baseline3_equiv = baseline60 * 3  # expected 3s volume under baseline rate

    coil = trail3 < 0.5 * baseline3_equiv
    coil_prev = coil.shift(1).fillna(False)  # "coil was just true" going into this second
    spike = sec_vol >= 3 * baseline60
    event = spike & coil_prev & baseline60.notna()

    # 3-min trailing price slope (causal): compare price now vs price 3 min ago
    px_3min_ago = sec_px.shift(180)
    trend_dir = np.sign(sec_px - px_3min_ago)

    event_times = sec_vol.index[event.fillna(False)]
    n_events = len(event_times)

    def fwd_return(t, horizon_s):
        try:
            p0 = sec_px.loc[t]
            t1 = t + pd.Timedelta(seconds=horizon_s)
            if t1 not in sec_px.index:
                return np.nan
            p1 = sec_px.loc[t1]
            if pd.isna(p0) or pd.isna(p1):
                return np.nan
            return p1 - p0
        except KeyError:
            return np.nan

    horizons = {"1min": 60, "3min": 180, "5min": 300}
    results = {}
    ev_returns = {h: [] for h in horizons}
    ev_hits = {h: [] for h in horizons}
    for t in event_times:
        td = trend_dir.get(t, np.nan)
        if pd.isna(td) or td == 0:
            continue
        for hname, hs in horizons.items():
            r = fwd_return(t, hs)
            if np.isnan(r):
                continue
            ev_returns[hname].append(r)
            ev_hits[hname].append(1 if np.sign(r) == td else 0)

    # baseline: random non-event points with valid trend, matched count
    valid_idx = sec_px.index[(~event.fillna(False)) & baseline60.notna() & trend_dir.notna() & (trend_dir != 0)]
    rng = np.random.default_rng(42)
    n_base = min(len(valid_idx), max(500, n_events * 20))
    base_times = rng.choice(valid_idx, size=n_base, replace=False) if len(valid_idx) > 0 else []
    base_returns = {h: [] for h in horizons}
    base_hits = {h: [] for h in horizons}
    for t in base_times:
        t = pd.Timestamp(t)
        td = trend_dir.get(t, np.nan)
        for hname, hs in horizons.items():
            r = fwd_return(t, hs)
            if np.isnan(r):
                continue
            base_returns[hname].append(r)
            base_hits[hname].append(1 if np.sign(r) == td else 0)

    print(f"Day: {DAY}")
    print(f"n_events (coil-prev + spike): {n_events}")
    for hname in horizons:
        eh = ev_hits[hname]
        bh = base_hits[hname]
        er = ev_returns[hname]
        br = base_returns[hname]
        print(f"--- {hname} ---")
        print(f"  event n={len(eh)}  hit_rate={np.mean(eh) if eh else np.nan:.3f}  avg_fwd_return={np.mean(er) if er else np.nan:.4f}")
        print(f"  base  n={len(bh)}  hit_rate={np.mean(bh) if bh else np.nan:.3f}  avg_fwd_return={np.mean(br) if br else np.nan:.4f}")


if __name__ == "__main__":
    main()
