#!/usr/bin/env python3
"""Absolute net levels (not just deltas) for v2-only vs v2+V3 across all 5
windows, to check v2's own `periods_nonneg_mean`-style criterion and verify
the config/strategy.yaml claim "julsep25/janmar26 remain net-negative overall
after blocking, just less so, not turned positive". Read-only.
"""
from __future__ import annotations

import sys
from copy import deepcopy

sys.path.insert(0, "src")

from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import (  # noqa: E402
    freeze_cell_book,
    SPECIALIZED_PATCHES,
    CELL_TUNE_V2_PATCHES,
    CELL_TUNE_V3_PATCHES,
)


def v2_only_book():
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    for sess, reg, upd in CELL_TUNE_V2_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


def v2_plus_v3_book():
    book = v2_only_book()
    for sess, reg, upd in CELL_TUNE_V3_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


def _arrays_from_rows(day, rows):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def window_net(cache, recipe, vix):
    days = list_days(cache)
    total = 0.0
    for d in days:
        rows = load_day(d, source=cache)
        if not rows:
            continue
        O, H, L, C, V, T = _arrays_from_rows(d, rows)
        trades, *_ = simulate(O, H, L, C, V, T, deepcopy(recipe), vix_delta=vix)
        total += sum(float(t["pnl"]) for t in (trades or []))
    return total, len(days)


def main():
    vix = load_vixtwn_delta() or {}
    before = PAPER_RECIPE.copy()
    before["session_pv_book"] = v2_only_book()
    after = PAPER_RECIPE

    windows = {
        "w83": "tx_1m_fullnight_cache_full.json",
        "julsep25": "tx_1m_julsep_holdout_cache.json",
        "octdec25": "tx_1m_octdec_holdout_cache.json",
        "janmar26": "tx_1m_janmar_holdout_cache.json",
    }
    nonneg_after = 0
    total_windows = 0
    for wname, cache in windows.items():
        nb, nd = window_net(cache, before, vix)
        na, _ = window_net(cache, after, vix)
        total_windows += 1
        if na >= 0:
            nonneg_after += 1
        print(f"{wname:10s} n_days={nd:3d}  before(v2-only)={nb:10.1f}  "
              f"after(v2+v3)={na:10.1f}  delta={na - nb:10.1f}  "
              f"after_nonneg={'Y' if na >= 0 else 'N'}")

    # w25 (subset of w83)
    days = list_days("tx_1m_fullnight_cache_full.json")[-25:]
    nb25 = na25 = 0.0
    for d in days:
        rows = load_day(d, source="tx_1m_fullnight_cache_full.json")
        if not rows:
            continue
        O, H, L, C, V, T = _arrays_from_rows(d, rows)
        tb, *_ = simulate(O, H, L, C, V, T, deepcopy(before), vix_delta=vix)
        ta, *_ = simulate(O, H, L, C, V, T, deepcopy(after), vix_delta=vix)
        nb25 += sum(float(t["pnl"]) for t in (tb or []))
        na25 += sum(float(t["pnl"]) for t in (ta or []))
    total_windows += 1
    if na25 >= 0:
        nonneg_after += 1
    print(f"{'w25':10s} n_days={len(days):3d}  before(v2-only)={nb25:10.1f}  "
          f"after(v2+v3)={na25:10.1f}  delta={na25 - nb25:10.1f}  "
          f"after_nonneg={'Y' if na25 >= 0 else 'N'}")

    print(f"\nperiods_nonneg_mean (after book): {nonneg_after}/{total_windows}")


if __name__ == "__main__":
    main()
