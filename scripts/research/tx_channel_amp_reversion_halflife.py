"""Item #12: quantify the half-life of amp30's mean reversion toward "today's
own running baseline" (z_extreme, from tx_channel_amp_persistence_drivers2.py).

Round 9 established EXISTENCE (|z_extreme| predicts imminent forecast
instability / large surprise). This script quantifies SPEED: given
|z_extreme[t]| in some bin, how many minutes until wamp[t+h] decays back to
within 50% of its deviation from the SAME anchored baseline mean(t)
("baseline" is fixed at the value it had at event time t, not updated
forward -- otherwise "decay" would be measuring a moving target).

Uses the engine bar cache (cache_store.load_day), not the tick cache, so we
get full night sessions across 4 windows: w83 (2026-04-01..07-31, 83d),
janmar holdout (2026 Q1, 55d), julsep holdout (2025 Jul-Sep, 65d), octdec
holdout (2025 Oct-Dec, 62d) -- these double as 4 different-period "regimes".

Sessions: day (08:45-14:59) and night, where night is STITCHED across the
calendar-date file boundary (15:00-23:59 of file D + 00:00-08:44 of file
D+1, since the raw per-file night bars are split by the JSON's calendar-date
key, not by actual trading session).

Day-clustered throughout: one half-life-summary number per session-instance,
then aggregate. Right-censored events (deviation never decays 50% before
session end) are reported separately, not silently dropped from the median.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
import pandas as pd

from tx_channel_amp_volume_interaction import WINDOW, weighted_amp30

from tmf_channel.cache_store import list_days, load_day

MIN_HIST = 10
Z_BINS = [(1.0, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 3.5), (3.5, np.inf)]
MAX_SEARCH = 200  # minutes forward to search for 50% decay

SOURCES = {
    "w83(Apr-Jul26)": "tx_1m_fullnight_cache_full.json",
    "janmar_holdout(Q1'26)": "tx_1m_janmar_holdout_cache.json",
    "julsep_holdout(Jul-Sep'25)": "tx_1m_julsep_holdout_cache.json",
    "octdec_holdout(Oct-Dec'25)": "tx_1m_octdec_holdout_cache.json",
}


def bars_to_df(bars: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(bars)
    return df


def running_mean_std(wamp: np.ndarray, min_hist: int = MIN_HIST):
    n = len(wamp)
    base_mean = np.full(n, np.nan)
    base_std = np.full(n, np.nan)
    hist = []
    for i in range(n):
        if not np.isnan(wamp[i]):
            if len(hist) >= min_hist:
                base_mean[i] = np.mean(hist)
                base_std[i] = np.std(hist)
            hist.append(wamp[i])
    return base_mean, base_std


def half_life_events(df: pd.DataFrame) -> list[tuple[float, int, bool]]:
    """Returns list of (|z0|, half_life_minutes_or_censor_bound, censored)."""
    if len(df) < WINDOW + MIN_HIST + 5:
        return []
    wamp = weighted_amp30(df)
    base_mean, base_std = running_mean_std(wamp)
    n = len(wamp)
    out = []
    for t in range(WINDOW + MIN_HIST, n - 1):
        if np.isnan(wamp[t]) or np.isnan(base_mean[t]) or base_std[t] <= 0:
            continue
        dev0 = wamp[t] - base_mean[t]
        z0 = dev0 / base_std[t]
        if abs(z0) < Z_BINS[0][0]:
            continue
        target = 0.5 * abs(dev0)
        hl = None
        limit = min(n - 1, t + MAX_SEARCH)
        for h in range(t + 1, limit + 1):
            if np.isnan(wamp[h]):
                continue
            dev_h = wamp[h] - base_mean[t]
            if abs(dev_h) <= target:
                hl = h - t
                break
        if hl is None:
            out.append((abs(z0), limit - t, True))
        else:
            out.append((abs(z0), hl, False))
    return out


def build_day_segment(bars: list[dict]) -> pd.DataFrame:
    day = [b for b in bars if b["sess"] == "day"]
    return bars_to_df(day)


def build_night_segments(source: str, days: list[str]) -> list[pd.DataFrame]:
    """Stitch night(D 15:00-23:59) + night(D+1 00:00-08:44)."""
    cache = {d: load_day(d, source=source) for d in days}
    segs = []
    for i, d in enumerate(days):
        bars_d = cache[d]
        tail = [b for b in bars_d if b["sess"] == "night" and b["t"] >= "15:00"]
        head = []
        if i + 1 < len(days):
            d2 = days[i + 1]
            bars_d2 = cache[d2]
            head = [b for b in bars_d2 if b["sess"] == "night" and b["t"] < "08:45"]
        seg = tail + head
        if len(seg) > WINDOW + MIN_HIST + 5:
            segs.append(bars_to_df(seg))
    return segs


def bin_label(z):
    for lo, hi in Z_BINS:
        if lo <= z < hi:
            return f"[{lo},{hi if hi != np.inf else 'inf'})"
    return None


def summarize(events, tag):
    if not events:
        print(f"{tag}: no events")
        return
    by_bin = {b: [] for b in [f"[{lo},{hi if hi!=np.inf else 'inf'})" for lo, hi in Z_BINS]}
    for z0, hl, censored in events:
        lbl = bin_label(z0)
        if lbl:
            by_bin[lbl].append((hl, censored))
    print(f"\n--- {tag} (n_events={len(events)}) ---")
    print(f"{'z_bin':>14} {'n':>6} {'censor%':>8} {'median_hl':>10} {'p25':>6} {'p75':>6} {'mean_hl':>8}")
    for lbl, vals in by_bin.items():
        if not vals:
            continue
        hls = np.array([v[0] for v in vals], dtype=float)
        cens = np.array([v[1] for v in vals])
        med = np.median(hls)
        p25 = np.percentile(hls, 25)
        p75 = np.percentile(hls, 75)
        print(f"{lbl:>14} {len(vals):>6} {cens.mean()*100:>7.1f}% {med:>10.1f} {p25:>6.1f} {p75:>6.1f} {hls.mean():>8.1f}")


def main():
    all_day_events = {}
    all_night_events = {}
    for tag, source in SOURCES.items():
        days = list_days(source)
        day_events = []
        night_events = []
        for d in days:
            bars = load_day(d, source=source)
            df_day = build_day_segment(bars)
            if len(df_day) > WINDOW + MIN_HIST + 5:
                day_events.extend(half_life_events(df_day))
        for seg in build_night_segments(source, days):
            night_events.extend(half_life_events(seg))
        all_day_events[tag] = day_events
        all_night_events[tag] = night_events
        summarize(day_events, f"{tag} DAY session")
        summarize(night_events, f"{tag} NIGHT session (stitched)")

    print("\n\n=== POOLED ACROSS ALL 4 WINDOWS ===")
    pooled_day = [e for v in all_day_events.values() for e in v]
    pooled_night = [e for v in all_night_events.values() for e in v]
    summarize(pooled_day, "ALL-WINDOWS DAY")
    summarize(pooled_night, "ALL-WINDOWS NIGHT")


if __name__ == "__main__":
    main()
