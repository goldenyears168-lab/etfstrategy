#!/usr/bin/env python3
"""BUG-1 self-test for the CELL_TUNE_V3 w83/w25 dual-window audit.

The shared research harness (tmf_channel.harness.run_day / cache_store.load_day)
simulates each calendar day independently (fresh `lots=[]` state) and the live
PAPER_RECIPE runs with eod_flatten=False (matches live: the poll must not
flatten mid-session). That means any position still open at the last bar of a
day's array produces a non-None `open_pos` with unrealized `u_pnl`, but that
u_pnl is NEVER added into `trades`/`net` for that day, and the position is NOT
carried into the next day's simulation (each day starts flat) -- so the PnL is
silently dropped, not merely deferred. This mirrors the BUG-1 pattern in
docs/research-integrity-checklist.md.

This script quantifies the gap for the v2-only vs v2+V3 comparison on w83:
for every day, force-close any open_pos at the day's own last close (honest
settlement), add u_pnl into that day's net, and recompute the w83/w25 dual-
window delta both ways to see if the accept verdict is robust to this gap.

Read-only: does not touch src/order/tmf_channel_pv16_book.py or config.
"""
from __future__ import annotations

import sys
from copy import deepcopy
from statistics import mean, stdev

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


def _arrays_from_rows(rows, day):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def day_net_and_openpos(rows, recipe, vix, day):
    O, H, L, C, V, T = _arrays_from_rows(rows, day)
    trades, events, ws, wl, rvol, regime, open_pos = simulate(
        O, H, L, C, V, T, deepcopy(recipe), vix_delta=vix
    )
    net = sum(float(t["pnl"]) for t in (trades or []))
    u_pnl = float(open_pos["u_pnl"]) if open_pos else 0.0
    n_open = int(open_pos["n"]) if open_pos else 0
    return net, u_pnl, n_open


def day_clustered_t(deltas):
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
    vix = load_vixtwn_delta() or {}
    before = PAPER_RECIPE.copy()
    before["session_pv_book"] = v2_only_book()
    after = PAPER_RECIPE  # already v2+v3 (live)

    days = list_days("tx_1m_fullnight_cache_full.json")
    assert len(days) == 83

    rows_net_b, rows_net_a = [], []
    rows_honest_b, rows_honest_a = [], []
    n_open_days_b = n_open_days_a = 0
    total_u_b = total_u_a = 0.0

    for d in days:
        rows = load_day(d, source="tx_1m_fullnight_cache_full.json")
        if not rows:
            continue
        nb, ub, nob = day_net_and_openpos(rows, before, vix, d)
        na, ua, noa = day_net_and_openpos(rows, after, vix, d)
        rows_net_b.append(nb)
        rows_net_a.append(na)
        rows_honest_b.append(nb + ub)
        rows_honest_a.append(na + ua)
        if nob:
            n_open_days_b += 1
            total_u_b += ub
        if noa:
            n_open_days_a += 1
            total_u_a += ua

    for w, days_slice_idx in (("w83", slice(None)), ("w25", slice(-25, None))):
        nb = rows_net_b[days_slice_idx]
        na = rows_net_a[days_slice_idx]
        hb = rows_honest_b[days_slice_idx]
        ha = rows_honest_a[days_slice_idx]
        delta_raw = [a - b for a, b in zip(na, nb)]
        delta_honest = [a - b for a, b in zip(ha, hb)]
        t_raw, p_raw = day_clustered_t(delta_raw)
        t_h, p_h = day_clustered_t(delta_honest)
        print(f"\n=== {w} ===")
        print(f"raw (no force-close): sum(before)={sum(nb):.1f} sum(after)={sum(na):.1f} "
              f"sum(delta)={sum(delta_raw):.1f} t={t_raw:.3f} p={p_raw:.4g}")
        print(f"honest (force-close open_pos at day's own last close): "
              f"sum(before)={sum(hb):.1f} sum(after)={sum(ha):.1f} "
              f"sum(delta)={sum(delta_honest):.1f} t={t_h:.3f} p={p_h:.4g}")

    print(f"\nopen_pos day-end frequency (out of 83 days): "
          f"before(v2-only)={n_open_days_b} days, total_u_pnl={total_u_b:.1f}; "
          f"after(v2+v3)={n_open_days_a} days, total_u_pnl={total_u_a:.1f}")


if __name__ == "__main__":
    main()
