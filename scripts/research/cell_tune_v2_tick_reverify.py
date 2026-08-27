#!/usr/bin/env python3
"""Tick-level reverify of the 4 largest-apparent-impact CELL_TUNE_V2_PATCHES
entries (src/order/tmf_channel_pv16_book.py), on w83
(2026-04-01..2026-07-31).

Selection of the 4 patches (by bar-level net delta vs baseline, read from
reports/research/channel_lab/r_cost_aware_cell_tune_synthesis.json 'all_rec'
variant, which contains 12 of the same patches as CELL_TUNE_V2_PATCHES plus
3 later-reverted ones -- reverted ones excluded from selection):
  1. night|normal        block=[L,S]   w83 cell net baseline -3684.7 -> 0   (+3684.7)
  2. night|div_hh_weak_vol block=[L,S] w83 cell net baseline -1188.8 -> 0   (+1188.8)
  3. day|contract        aggressive hang retune  w83 cell net 1139.0->2600.4 (+1461.4)
  4. day|normal          aggressive hang/gamma/max_hold retune 5246.8->7023.1 (+1776.3)

Method: hold the FULL CELL_TUNE_V2_PATCHES book fixed and ablate ONE patch at
a time (revert that single cell to its SPECIALIZED_PATCHES-only state), so
the marginal effect of each patch is measured with realistic single-slot
(max_lots=1) cascading effects from all OTHER v2 patches already in place --
not a naive with/without-all comparison.

For each day: keep night rows from the w83 fullnight bar cache, replace
day-session rows with 1-min bars resampled from front-month-filtered ticks
(fallback to cache bar when no tick match). True re-simulation via
tmf_channel.engine.simulate() on both bar-level cache rows and tick-rebuilt
hybrid rows. Day-clustered paired t-test on (full_v2 - ablated) delta.
"""
from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from statistics import mean, stdev

sys.path.insert(0, "src")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmf_channel.cache_store import load_day, list_days  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate, summarize  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import (  # noqa: E402
    freeze_cell_book,
    SPECIALIZED_PATCHES,
    CELL_TUNE_V2_PATCHES,
    CELL_TUNE_V3_PATCHES,
)
from tx_channel_tick_validation import load_front_month_ticks, resample_to_1min  # noqa: E402

CACHE_SRC = "tx_1m_fullnight_cache_full.json"

# The 4 patches under reverify, keyed by (sess, reg) -> matches an entry in
# CELL_TUNE_V2_PATCHES.
TARGETS = [
    ("night", "normal"),
    ("night", "div_hh_weak_vol"),
    ("day", "contract"),
    ("day", "normal"),
]


def base_v2_book():
    """freeze + SPECIALIZED_PATCHES + CELL_TUNE_V2_PATCHES (no v3, since v3
    blocks day|normal entirely which would swallow the day|normal ablation
    target -- reverify v2 in isolation, matching how it was originally
    graduated before v3 existed)."""
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    for sess, reg, upd in CELL_TUNE_V2_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


def ablated_book(target_sess: str, target_reg: str):
    """Full v2 book, but target cell reverted to its SPECIALIZED_PATCHES-only
    (pre-v2) state."""
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    for sess, reg, upd in CELL_TUNE_V2_PATCHES:
        if (sess, reg) == (target_sess, target_reg):
            continue
        book[sess][reg].update(deepcopy(upd))
    return book


def build_hybrid_rows(day: str, cache_rows: list[dict]) -> tuple[list[dict], dict]:
    ticks = load_front_month_ticks(day)
    stats = dict(day_rows=0, replaced=0, fallback=0, had_ticks=ticks is not None)
    if ticks is None or ticks.empty:
        return cache_rows, stats
    tb = resample_to_1min(ticks)
    if tb.empty:
        return cache_rows, stats
    tb_by_hhmm = {row["Datetime"].strftime("%H:%M"): row for _, row in tb.iterrows()}
    out = []
    for r in cache_rows:
        if r["sess"] != "day":
            out.append(r)
            continue
        stats["day_rows"] += 1
        hhmm = r["t"]
        tr = tb_by_hhmm.get(hhmm)
        if tr is None:
            stats["fallback"] += 1
            out.append(r)
            continue
        stats["replaced"] += 1
        out.append(dict(
            t=hhmm, o=float(tr["Open"]), h=float(tr["High"]),
            l=float(tr["Low"]), c=float(tr["Close"]), v=float(tr["Volume"]),
            sess="day",
        ))
    return out, stats


def sim_net(day: str, rows: list[dict], session_book: dict, vix: dict) -> tuple[float, int]:
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    recipe = deepcopy(PAPER_RECIPE)
    recipe["session_pv_book"] = session_book
    recipe.setdefault("hang_anchor", "O")
    trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    s = summarize(trades) if trades else {"n": 0, "net": 0.0}
    return float(s.get("net") or 0.0), int(s.get("n") or 0)


def day_clustered_t(deltas: list[float]) -> tuple[float, float]:
    n = len(deltas)
    if n < 2:
        return float("nan"), float("nan")
    dmean = mean(deltas)
    dsd = stdev(deltas)
    se = dsd / (n ** 0.5)
    t = dmean / se if se > 0 else float("inf")
    try:
        from scipy import stats
        p = 2 * (1 - stats.t.cdf(abs(t), df=n - 1))
    except ImportError:
        p = float("nan")
    return t, p


def main():
    full_book = base_v2_book()
    vix = load_vixtwn_delta() or {}
    days = list_days(source=CACHE_SRC)
    print(f"w83: {days[0]}..{days[-1]} n={len(days)} (cache={CACHE_SRC})")

    for target in TARGETS:
        abl_book = ablated_book(*target)
        bar_deltas, tick_deltas = [], []
        n_tick_days = 0
        total_day_rows = total_replaced = total_fallback = 0
        for d in days:
            rows = load_day(d, source=CACHE_SRC)
            if not rows:
                continue
            nb_full, _ = sim_net(d, rows, full_book, vix)
            nb_abl, _ = sim_net(d, rows, abl_book, vix)
            bar_deltas.append(nb_full - nb_abl)

            hybrid, stats = build_hybrid_rows(d, rows)
            if stats["had_ticks"] and stats["replaced"] > 0:
                n_tick_days += 1
                total_day_rows += stats["day_rows"]
                total_replaced += stats["replaced"]
                total_fallback += stats["fallback"]
                nt_full, _ = sim_net(d, hybrid, full_book, vix)
                nt_abl, _ = sim_net(d, hybrid, abl_book, vix)
                tick_deltas.append(nt_full - nt_abl)

        bt, bp = day_clustered_t(bar_deltas)
        print(f"\n=== patch: {target[0]}|{target[1]} ===")
        print(f"  BAR-LEVEL:  n_days={len(bar_deltas)} sum(delta)={sum(bar_deltas):.1f} "
              f"mean={mean(bar_deltas):.2f} t={bt:.3f} p={bp:.4g}")
        if tick_deltas:
            tt, tp = day_clustered_t(tick_deltas)
            cov = 100.0 * total_replaced / total_day_rows if total_day_rows else 0.0
            print(f"  TICK-REBUILT: n_days={n_tick_days} sum(delta)={sum(tick_deltas):.1f} "
                  f"mean={mean(tick_deltas):.2f} t={tt:.3f} p={tp:.4g} "
                  f"(day-bar tick-coverage={cov:.1f}%, fallback={total_fallback}/{total_day_rows})")
        else:
            print("  TICK-REBUILT: no tick data available")


if __name__ == "__main__":
    main()
