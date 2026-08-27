#!/usr/bin/env python3
"""Validate CELL_TUNE_V3_PATCHES (day|normal + day|div_hh_weak_vol full block)
on the "w25" window — closing the graduation-gap noted in
src/order/tmf_channel_pv16_book.py's v1.4.0 docstring.

w25 investigation finding (grepped reports/research/channel_lab/r_cost_aware_cell_tune_wf.py
and .json): "w25" is NOT an independent OOS season. It is defined as
``days_main[-25:]`` where ``days_main`` = sorted keys of
reports/research/channel_lab/tx_1m_daynight_cache.json, an 83-day cache
(2026-04-01..2026-07-31) that is content-identical (same day range) to the
engine's own ``tx_1m_fullnight_cache_full.json`` ("w83"). So w25 = the most
recent 25 trading days *within* w83: 2026-06-26..2026-07-31. It was used in
the v2 graduation as a recency-weighted checkpoint ("does the tune still work
in the freshest slice of the in-sample window"), not a separate holdout.

This script reconstructs that exact 25-day slice from
tmf_channel.cache_store (tx_1m_fullnight_cache_full.json) and runs the same
before(V2-only)/after(V2+V3) harness comparison already done for full w83 and
the 3 holdout caches, with a day-clustered paired t-test on the per-day delta.
"""
from __future__ import annotations

import sys
from copy import deepcopy
from statistics import mean, stdev

sys.path.insert(0, "src")

from tmf_channel.cache_store import list_days  # noqa: E402
from tmf_channel.harness import run_days, summarize_days  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import (  # noqa: E402
    CELL_TUNE_V3_PATCHES,
    freeze_cell_book,
    SPECIALIZED_PATCHES,
    CELL_TUNE_V2_PATCHES,
)

CACHE = "tx_1m_fullnight_cache_full.json"


def v2_only_book():
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    for sess, reg, upd in CELL_TUNE_V2_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


def main():
    days = list_days(source=CACHE)
    assert len(days) == 83, len(days)
    w25 = days[-25:]
    print(f"w25 window: {w25[0]} .. {w25[-1]} (n={len(w25)})")
    assert w25[0] == "2026-06-26" and w25[-1] == "2026-07-31"

    before = deepcopy(PAPER_RECIPE)
    before["session_pv_book"] = v2_only_book()  # V2 only, no V3 block
    after = PAPER_RECIPE  # already includes V3 block (live baseline)

    rows_before = run_days(w25, recipe=before, cache_name=CACHE)
    rows_after = run_days(w25, recipe=after, cache_name=CACHE)

    sb = summarize_days(rows_before)
    sa = summarize_days(rows_after)
    print("BEFORE (V2 only):", sb)
    print("AFTER  (V2+V3): ", sa)

    by_day_b = {r["day"]: r["net"] for r in rows_before if r.get("ok")}
    by_day_a = {r["day"]: r["net"] for r in rows_after if r.get("ok")}
    common = [d for d in w25 if d in by_day_b and d in by_day_a]
    deltas = [by_day_a[d] - by_day_b[d] for d in common]
    n = len(deltas)
    dmean = mean(deltas)
    dsd = stdev(deltas) if n > 1 else 0.0
    se = dsd / (n**0.5) if n > 1 else 0.0
    t = dmean / se if se > 0 else float("inf")
    print(f"\nn_days={n} sum(delta)={sum(deltas):.1f} mean(delta)={dmean:.2f} "
          f"sd={dsd:.2f} t={t:.3f}")
    # two-sided p-value via normal approx (n=25, close enough to report; also
    # print via scipy if available for exact t-dist p)
    try:
        from scipy import stats

        p = 2 * (1 - stats.t.cdf(abs(t), df=n - 1)) if n > 1 else float("nan")
        print(f"day-clustered paired t-test: t({n-1})={t:.3f} p={p:.4g}")
    except ImportError:
        print("(scipy unavailable — t-stat above only)")

    print(f"\ndelta net total: {sa['net'] - sb['net']:.1f}")
    print(f"days improved: {sum(1 for x in deltas if x > 0)}/{n}")


if __name__ == "__main__":
    main()
