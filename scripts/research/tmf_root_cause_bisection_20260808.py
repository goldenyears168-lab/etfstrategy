#!/usr/bin/env python3
"""Root-cause bisection (2026-08-08 night): tonight's true re-simulation of
the literal current PAPER_RECIPE gives pooled mean=-37.2 pt/day across the
4 sanctioned windows (n=265) -- flatly contradicting an earlier-session claim
of pooled mean=+38.3 pt/day for "current live baseline" that this script's
author could not reproduce or trace to a saved artifact. Independently
reproduced 3x tonight (direct debug, tmf_simplify_v2_vs_v120_baseline_resim.py,
and cross-checked against audit_cell_tune_v3_absolute_levels.py's
"after(v2-only) fixed" w83 sum of -3605.0, which matches exactly).

This script bisects along the axes that plausibly explain the gap: cell-book
depth (raw freeze / v1.2.0 / v1.3.0-v2), eod_flatten (day-isolated
force-close vs let-ride), and vixtwn_calib (none vs blend), all on the
SAME current cache data (tx_1m_fullnight_cache_full.json + 3 holdouts) and
SAME current market_vix_daily table content, to find which axis the
discrepancy actually lives on -- and, per user request, to find the single
most load-bearing simplification axis.

Does NOT touch src/order/, config/order.yaml, .env, launchd/, scripts/order/.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import (  # noqa: E402
    freeze_cell_book,
    SPECIALIZED_PATCHES,
    CELL_TUNE_V2_PATCHES,
)
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402

SOURCES = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}


def raw_book():
    return freeze_cell_book()


def v120_book():
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    return book


def v13_v2_book():
    book = v120_book()
    for sess, reg, upd in CELL_TUNE_V2_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


BOOKS = {"raw": raw_book, "v120": v120_book, "v13_v2_live": v13_v2_book}


def day_arrays(day, rows):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def load_all_days():
    cache = {}
    for label, src in SOURCES.items():
        days = list_days(source=src)
        cache[label] = [(d, day_arrays(d, load_day(d, source=src))) for d in days if load_day(d, source=src)]
    return cache


def main():
    vix_real = load_vixtwn_delta() or {}
    all_days = load_all_days()
    total_days = sum(len(v) for v in all_days.values())
    print(f"loaded {total_days} days across {len(all_days)} windows", flush=True)

    results = []
    for book_name, book_fn in BOOKS.items():
        for eod in (False, True):
            for vixtwn in ("none", "blend"):
                recipe = dict(PAPER_RECIPE)
                recipe["session_pv_book"] = book_fn()
                recipe["eod_flatten"] = eod
                recipe["vixtwn_calib"] = vixtwn
                recipe.setdefault("hang_anchor", "O")

                pooled = []
                for label, days in all_days.items():
                    for d, arrs in days:
                        trades, *_ = simulate(*arrs, recipe, vix_delta=vix_real)
                        pooled.append(sum(t["pnl"] for t in trades))
                mean_p = st.mean(pooled)
                std_p = st.stdev(pooled)
                row = dict(
                    book=book_name, eod_flatten=eod, vixtwn=vixtwn,
                    n=len(pooled), mean=round(mean_p, 2), std=round(std_p, 2),
                    sum=round(sum(pooled), 1),
                )
                results.append(row)
                print(row, flush=True)

    out_path = "reports/research/channel_lab/tmf_root_cause_bisection_20260808_result.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
