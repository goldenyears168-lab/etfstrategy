#!/usr/bin/env python3
"""PV8 classifier dwell-time / flicker characterization (research, read-only).

Context: 2026-08-08 background monitor found the live TMF order layer's PV8
regime classifier flickering rapidly between fully-blocked cells (e.g.
night|climax_up, night|normal, night|div_hh_weak_vol -- all block=['L','S'])
and adjacent unblocked cells (night|contract, night|expand_up, night|dry),
producing wasteful hard place/cancel churn on the block side (never a capital
risk -- block fires correctly every time -- but real API churn and a possible
sign the PV8 classifier itself is unstable near certain regime-pair
boundaries).

This script answers "characterize phase, part 1": is the instability visible
in pure historical backtest data at all, using the frozen live engine
(tmf_channel.causal_engine via the engine.py facade) run with the current
live PAPER_RECIPE (recipe_version=final_v1_4_0_pv16_normal_dhwv_block), over
the 4 sanctioned validation windows (w83, julsep25, octdec25, janmar26)?

Methodology note (explicit per repo convention): this is purely descriptive
characterization of historical classifier behavior, NOT a live causal
decision rule -- full-day/full-window descriptive statistics (medians,
percentiles, counts) are appropriate here and are not being used to gate any
live decision. No fix is proposed by this script.

Session-container convention: each cache "day" key is a single
simulate()-call container holding up to 3 real-time-contiguous segments
(00:00-04:59 night tail, 08:45-13:45 day, 15:00-23:59 night head) with an
authoritative `sess` field already carried by cache_store rows. Runs are
grouped within each (day, sess-segment) container and are NOT stitched across
day-container boundaries (matches scripts/research/tx_channel_daynight_split.py's
existing per-container convention) -- checked empirically: cross-midnight
close-to-open deltas between adjacent calendar-day containers in these caches
are NOT small (e.g. 2026-04-01 23:59 close 33815 -> 2026-04-02 00:00 open
32738, a ~3.2% "gap" within a nominal single minute), so treating them as one
continuous run would be an unjustified assumption, not a conservative one.
This means night-session dwell/flicker stats are a mild UNDERSTATEMENT of any
true continuity spanning midnight (a run that is still open at 23:59 and
still the same label at the next day's 00:00 is counted as 2 runs, not 1) --
noted explicitly, not hidden.

'na' bars (classify_pv warmup, first ~10 bars of a day's array) are dropped
before run-grouping, they carry no engine decision.
"""
from __future__ import annotations

import statistics as stats
from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any

from order.tmf_channel_config import PAPER_RECIPE
from order.tmf_channel_pv16_book import PV8, RECIPE_VERSION, cell_recipe, specialized_cell_book
from tmf_channel.cache_store import list_days, load_day
from tmf_channel.engine import load_vixtwn_delta, simulate

WINDOWS: list[tuple[str, str]] = [
    ("w83", "tx_1m_fullnight_cache_full.json"),
    ("julsep25", "tx_1m_julsep_holdout_cache.json"),
    ("octdec25", "tx_1m_octdec_holdout_cache.json"),
    ("janmar26", "tx_1m_janmar_holdout_cache.json"),
]

BOOK = specialized_cell_book()
FULLY_BLOCKED: set[tuple[str, str]] = set()
PARTIAL_BLOCKED: set[tuple[str, str]] = set()
for _sess in ("day", "night"):
    for _pv in PV8:
        _c = cell_recipe(BOOK, session=_sess, pv=_pv)
        _b = set(_c.get("block") or [])
        if _b == {"L", "S"}:
            FULLY_BLOCKED.add((_sess, _pv))
        elif _b:
            PARTIAL_BLOCKED.add((_sess, _pv))


def is_blocked(sess: str, pv: str) -> bool:
    """Fully blocked = block==['L','S'] per task definition. Partial
    (single-side) blocks count as NOT blocked in this binary split, flagged
    separately."""
    return (sess, pv) in FULLY_BLOCKED


def _arrays_from_rows(day: str, rows: list[dict[str, Any]]):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def _segments_for_day(rows: list[dict[str, Any]], regime: list[str]) -> list[tuple[str, list[str]]]:
    """Split one day-container's bars into contiguous same-sess segments."""
    segs: list[tuple[str, list[str]]] = []
    cur_sess: str | None = None
    cur: list[str] = []
    for r, pv in zip(rows, regime):
        sess = r.get("sess")
        if sess not in ("day", "night"):
            if cur:
                segs.append((cur_sess, cur))
            cur_sess, cur = None, []
            continue
        if sess != cur_sess:
            if cur:
                segs.append((cur_sess, cur))
            cur_sess, cur = sess, []
        cur.append(pv)
    if cur:
        segs.append((cur_sess, cur))
    return segs


def _pv_runs(pv_list: list[str]) -> list[tuple[str, int]]:
    """Contiguous same-label runs (label, length_in_bars); 'na' bars dropped
    (they break contiguity but are not emitted as runs themselves)."""
    runs: list[tuple[str, int]] = []
    i, n = 0, len(pv_list)
    while i < n:
        if pv_list[i] == "na":
            i += 1
            continue
        j = i + 1
        while j < n and pv_list[j] == pv_list[i]:
            j += 1
        runs.append((pv_list[i], j - i))
        i = j
    return runs


def _blocked_runs(sess: str, pv_list: list[str]) -> list[tuple[bool, int]]:
    """Contiguous same-blocked-boolean runs (blocked, length_in_bars), na dropped."""
    runs: list[tuple[bool, int]] = []
    i, n = 0, len(pv_list)
    while i < n:
        if pv_list[i] == "na":
            i += 1
            continue
        b0 = is_blocked(sess, pv_list[i])
        j = i + 1
        while j < n and pv_list[j] != "na" and is_blocked(sess, pv_list[j]) == b0:
            j += 1
        runs.append((b0, j - i))
        i = j
    return runs


def _pctile(xs: list[int], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = q * (len(s) - 1)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return s[f] + (s[c] - s[f]) * (k - f)


def analyze_window(name: str, cache_name: str) -> dict[str, Any]:
    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")
    recipe.setdefault("recipe_version", RECIPE_VERSION)
    vix = load_vixtwn_delta() or {}

    dwell: dict[tuple[str, bool], list[int]] = defaultdict(list)
    pair_flicker: dict[str, Counter] = {"day": Counter(), "night": Counter()}
    blocked_entry_len: dict[str, list[int]] = {"day": [], "night": []}
    n_days = 0

    for day in list_days(cache_name):
        rows = load_day(day, source=cache_name)
        if not rows:
            continue
        O, H, L, C, V, T = _arrays_from_rows(day, rows)
        _trades, _events, _ws, _wl, _rvol, regime, _open_pos = simulate(
            O, H, L, C, V, T, recipe, vix_delta=vix
        )
        n_days += 1
        for sess, pv_list in _segments_for_day(rows, regime):
            if sess not in ("day", "night"):
                continue
            # (1) dwell-time distribution by pv-label run, split blocked/not
            runs = _pv_runs(pv_list)
            for label, length in runs:
                dwell[(sess, is_blocked(sess, label))].append(length)

            # (2) flicker pairs: short round-trip excursions P -> X -> P
            # where dwell(X) <= 2 bars ("under ~3 minutes")
            for i in range(1, len(runs) - 1):
                prev_label, _prev_len = runs[i - 1]
                cur_label, cur_len = runs[i]
                next_label, _next_len = runs[i + 1]
                if prev_label == next_label and prev_label != cur_label and cur_len <= 2:
                    key = "<->".join(sorted((prev_label, cur_label)))
                    pair_flicker[sess][key] += 1

            # (3) fraction of blocked-boolean entries reverting <2 min
            bruns = _blocked_runs(sess, pv_list)
            for idx, (blocked, length) in enumerate(bruns):
                if blocked and idx > 0:  # has a preceding non-blocked run (entry event)
                    blocked_entry_len[sess].append(length)

    def dwell_stats(xs: list[int]) -> dict[str, Any]:
        if not xs:
            return {"n": 0}
        return {
            "n": len(xs),
            "median": _pctile(xs, 0.5),
            "p10": _pctile(xs, 0.10),
            "p25": _pctile(xs, 0.25),
            "mean": round(stats.fmean(xs), 2),
        }

    dwell_report = {
        f"{sess}_{'blocked' if blk else 'nonblocked'}": dwell_stats(dwell.get((sess, blk), []))
        for sess in ("day", "night")
        for blk in (True, False)
    }

    flicker_report = {
        sess: pair_flicker[sess].most_common(5) for sess in ("day", "night")
    }

    entry_frac = {}
    for sess in ("day", "night"):
        xs = blocked_entry_len[sess]
        under2 = sum(1 for x in xs if x < 2)
        entry_frac[sess] = {
            "n_entries": len(xs),
            "under_2min": under2,
            "frac_under_2min": round(under2 / len(xs), 3) if xs else None,
            "persist_ge_2min": len(xs) - under2,
        }

    return {
        "window": name,
        "cache": cache_name,
        "n_days": n_days,
        "dwell": dwell_report,
        "flicker_pairs": flicker_report,
        "blocked_entry_reversion": entry_frac,
        "_raw_dwell": dwell,
        "_raw_blocked_entry_len": blocked_entry_len,
    }


def main() -> None:
    results = [analyze_window(name, cache) for name, cache in WINDOWS]

    # pooled across all 4 windows
    pooled_dwell: dict[tuple[str, bool], list[int]] = defaultdict(list)
    pooled_entry_len: dict[str, list[int]] = {"day": [], "night": []}
    pooled_flicker: dict[str, Counter] = {"day": Counter(), "night": Counter()}
    for r in results:
        for k, v in r["_raw_dwell"].items():
            pooled_dwell[k].extend(v)
        for sess in ("day", "night"):
            pooled_entry_len[sess].extend(r["_raw_blocked_entry_len"][sess])

    import json

    def dwell_stats(xs):
        if not xs:
            return {"n": 0}
        return {
            "n": len(xs),
            "median": _pctile(xs, 0.5),
            "p10": _pctile(xs, 0.10),
            "p25": _pctile(xs, 0.25),
            "mean": round(stats.fmean(xs), 2),
        }

    pooled_dwell_report = {
        f"{sess}_{'blocked' if blk else 'nonblocked'}": dwell_stats(pooled_dwell.get((sess, blk), []))
        for sess in ("day", "night")
        for blk in (True, False)
    }
    pooled_entry_frac = {}
    for sess in ("day", "night"):
        xs = pooled_entry_len[sess]
        under2 = sum(1 for x in xs if x < 2)
        pooled_entry_frac[sess] = {
            "n_entries": len(xs),
            "under_2min": under2,
            "frac_under_2min": round(under2 / len(xs), 3) if xs else None,
            "persist_ge_2min": len(xs) - under2,
        }

    out = {
        "recipe_version": RECIPE_VERSION,
        "fully_blocked_cells": sorted(f"{s}|{p}" for s, p in FULLY_BLOCKED),
        "partial_blocked_cells": sorted(f"{s}|{p}" for s, p in PARTIAL_BLOCKED),
        "per_window": [
            {k: v for k, v in r.items() if not k.startswith("_raw")} for r in results
        ],
        "pooled_dwell": pooled_dwell_report,
        "pooled_blocked_entry_reversion": pooled_entry_frac,
    }
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
