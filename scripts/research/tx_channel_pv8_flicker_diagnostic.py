#!/usr/bin/env python3
"""PV8 flicker diagnostic — characterize classify_pv() behavior around the
observed live flicker (night|climax_up / div_hh_weak_vol / normal <-> night|
contract / expand_up / dry) using w83 cache data.

Pure descriptive/historical characterization (rule 1 in the task): no live
causal filter is being built here, so a simple full-day pass is fine.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.causal_engine import (  # noqa: E402
    CLIMAX,
    CONTRACT,
    DRY,
    EXPAND,
    VOL_WIN,
    classify_pv,
    rvol_series,
)

SOURCE = "tx_1m_fullnight_cache_full.json"

BLOCKED = {"climax_up", "climax_dn", "div_hh_weak_vol", "normal"}
UNBLOCKED_ADJ = {"contract", "expand_up", "expand_dn", "dry"}


def session_of(bar) -> str:
    sess = bar.get("sess")
    if sess:
        return sess
    hm = str(bar["t"])[11:16]
    return "day" if "08:45" <= hm <= "13:45" else "night"


def main():
    days = list_days(SOURCE)
    episodes_found = []
    for day in days:
        bars = load_day(day, source=SOURCE)
        if not bars:
            continue
        O = [b["o"] for b in bars]
        H = [b["h"] for b in bars]
        L = [b["l"] for b in bars]
        C = [b["c"] for b in bars]
        V = [b["v"] for b in bars]
        rv = rvol_series(V, win=VOL_WIN)
        labels = []
        for t in range(len(bars)):
            lbl, impulse = classify_pv(C, O, rv, t)
            labels.append(lbl)

        # scan for flicker: consecutive bars where session=night and label
        # switches between BLOCKED and UNBLOCKED_ADJ sets within a short window
        for t in range(2, len(bars) - 1):
            if session_of(bars[t]) != "night":
                continue
            prev, cur, nxt = labels[t - 1], labels[t], labels[t + 1]
            if prev in BLOCKED and cur in UNBLOCKED_ADJ:
                episodes_found.append((day, t))
            elif prev in UNBLOCKED_ADJ and cur in BLOCKED:
                episodes_found.append((day, t))

    print(f"total flicker-boundary bars (night, blocked<->adjacent unblocked): {len(episodes_found)}")
    # group by day, pick days with most flicker
    from collections import Counter

    by_day = Counter(d for d, _ in episodes_found)
    top_days = by_day.most_common(5)
    print("top flicker days:", top_days)

    # dump detail for the top 2-3 days around the flicker bars
    for day, _ in top_days[:3]:
        bars = load_day(day, source=SOURCE)
        O = [b["o"] for b in bars]
        H = [b["h"] for b in bars]
        L = [b["l"] for b in bars]
        C = [b["c"] for b in bars]
        V = [b["v"] for b in bars]
        rv = rvol_series(V, win=VOL_WIN)
        labels = []
        impulses = []
        for t in range(len(bars)):
            lbl, imp = classify_pv(C, O, rv, t)
            labels.append(lbl)
            impulses.append(imp)

        idxs = sorted({t for d, t in episodes_found if d == day})
        print(f"\n=== day {day}: {len(idxs)} flicker-boundary bars ===")
        # cluster consecutive/close indices into episodes
        clusters = []
        cur_cluster = [idxs[0]]
        for i in idxs[1:]:
            if i - cur_cluster[-1] <= 5:
                cur_cluster.append(i)
            else:
                clusters.append(cur_cluster)
                cur_cluster = [i]
        clusters.append(cur_cluster)

        for cl in clusters[:3]:
            center = cl[len(cl) // 2]
            lo = max(0, center - 4)
            hi = min(len(bars), center + 5)
            print(f"  -- episode around bar {center} ({bars[center]['t']}) --")
            for t in range(lo, hi):
                marker = " <-- flip" if t in idxs else ""
                print(
                    f"    t={t} ts={bars[t]['t']} sess={session_of(bars[t])} "
                    f"V={V[t]:.0f} rv={rv[t]:.4f} C={C[t]:.1f} O={O[t]:.1f} "
                    f"impulse={impulses[t]:+.1f} label={labels[t]}{marker}"
                )
        print(
            f"  thresholds: CLIMAX={CLIMAX} EXPAND={EXPAND} CONTRACT={CONTRACT} DRY={DRY}"
        )


if __name__ == "__main__":
    main()
