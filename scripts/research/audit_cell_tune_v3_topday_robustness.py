#!/usr/bin/env python3
"""Top-day dominance robustness check for the w83 v2-only vs v2+V3 delta
(+12,476pt). Drops the top-1/3/5 best-delta days and recomputes the sum, to
check the improvement isn't a handful-of-days artifact (checklist BUG-6
appendix pattern, applied here to a delta series rather than a raw PnL
series). Read-only.
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


def _arrays_from_rows(day, rows):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def main():
    vix = load_vixtwn_delta() or {}
    before = PAPER_RECIPE.copy()
    before["session_pv_book"] = v2_only_book()
    after = PAPER_RECIPE

    days = list_days("tx_1m_fullnight_cache_full.json")
    per_day = []
    for d in days:
        rows = load_day(d, source="tx_1m_fullnight_cache_full.json")
        if not rows:
            continue
        O, H, L, C, V, T = _arrays_from_rows(d, rows)
        tb, *_ = simulate(O, H, L, C, V, T, deepcopy(before), vix_delta=vix)
        ta, *_ = simulate(O, H, L, C, V, T, deepcopy(after), vix_delta=vix)
        nb = sum(float(t["pnl"]) for t in (tb or []))
        na = sum(float(t["pnl"]) for t in (ta or []))
        per_day.append((d, na - nb))

    deltas = [x[1] for x in per_day]
    total = sum(deltas)
    sorted_desc = sorted(per_day, key=lambda x: x[1], reverse=True)
    print(f"w83 total delta = {total:.1f} over {len(deltas)} days")
    print(f"days with positive delta: {sum(1 for x in deltas if x > 0)}/{len(deltas)}")
    print(f"days with negative delta: {sum(1 for x in deltas if x < 0)}/{len(deltas)}")
    print(f"top-5 single-day deltas: {sorted_desc[:5]}")
    print(f"worst-5 single-day deltas: {sorted_desc[-5:]}")
    for k in (1, 3, 5, 10):
        excl = total - sum(x[1] for x in sorted_desc[:k])
        print(f"total excl. top-{k} best-delta day(s): {excl:.1f} "
              f"({'still positive' if excl > 0 else 'flips negative/zero'})")


if __name__ == "__main__":
    main()
