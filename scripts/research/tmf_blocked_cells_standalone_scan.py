#!/usr/bin/env python3
"""2026-08-11: user wants MORE independent validated cells (not touching
max_lots). Systematically re-screen every currently-blocked cell in the
LIVE book under the current recipe + the NOW-FIXED continuous NQ gate
(the original block decisions for several of these predate both the
continuous gate and tonight's forming-bar fix -- same situation that made
night|normal worth re-checking).

For each blocked cell (or blocked side), compute its OWN standalone net
pnl (trades it WOULD generate if unblocked) plus L/S side breakdown, so a
promising asymmetric candidate can be spotted immediately -- same method
that found night|normal's clean L/S split. Screens IS(22d) + OOS(66d);
report is diagnostic only, nothing deployed from this pass.
"""
from __future__ import annotations

import sys
from copy import deepcopy

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})
IS_DAYS = JULY_DAYS + AUG_DAYS

# (session, pv) pairs that are currently block=["L","S"] or block=["L"] etc,
# i.e. anything with a non-empty block list right now.
BLOCKED_CELLS = [
    ("day", "expand_up"), ("day", "expand_dn"),
    ("night", "climax_up"), ("night", "climax_dn"),
    ("night", "expand_dn"), ("night", "normal"), ("night", "div_hh_weak_vol"),
]


def load_arrays(day, source_map):
    source = source_map.get(day, "tx_1m_fullnight_cache_full.json")
    rows = load_day(day, source=source)
    if not rows:
        return None
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = bar_timestamps(day, rows, source=source)
    return O, H, L, C, V, T


def unblocked_book():
    """Every cell fully unblocked, so simulate() actually generates the
    trades each cell WOULD take -- we then attribute per-cell/per-side."""
    book = deepcopy(specialized_cell_book())
    for sess, pv in BLOCKED_CELLS:
        book[sess][pv]["block"] = []
    return book


def run_window(label, days, source_map, recipe_base, vix, book):
    stats = {(sess, pv): {"L": [0.0, 0], "S": [0.0, 0]} for sess, pv in BLOCKED_CELLS}
    for d in days:
        arr = load_arrays(d, source_map)
        if arr is None:
            continue
        gate = continuous_gate_for_day(d, arr[5], source=SOURCE_FOR_DAY.get(d, "tx_1m_fullnight_cache_full.json"))
        recipe = deepcopy(recipe_base)
        recipe["session_side_gate"] = gate
        recipe["session_pv_book"] = book
        O, H, L, C, V, T = arr
        trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
        for t in trades:
            reg = t.get("regime_e")
            et = str(t.get("et") or "")
            hm = et.split("T", 1)[1][:5] if "T" in et else et[:5]
            sess = "day" if "08:45" <= hm < "13:45" else "night"
            key = (sess, reg)
            if key not in stats:
                continue
            s = t["s"]
            stats[key][s][0] += t["pnl"]
            stats[key][s][1] += 1

    print(f"=== {label} ===")
    for key, sides in stats.items():
        l_net, l_n = round(sides["L"][0], 1), sides["L"][1]
        s_net, s_n = round(sides["S"][0], 1), sides["S"][1]
        print(f"{key}: L=({l_net},{l_n}) S=({s_net},{s_n}) total=({round(l_net+s_net,1)},{l_n+s_n})")
    return stats


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")

    book = unblocked_book()

    is_stats = run_window("IS_22d", IS_DAYS, SOURCE_FOR_DAY, recipe_base, vix, book)
    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]
    oos_source_map = {d: "tx_1m_fullnight_cache_full.json" for d in oos_days}
    oos_stats = run_window("OOS_66d", oos_days, oos_source_map, recipe_base, vix, book)

    print("\n=== SUMMARY: cells/sides positive in BOTH windows (candidates) ===")
    for key in BLOCKED_CELLS:
        is_l, is_s = is_stats[key]["L"][0], is_stats[key]["S"][0]
        oos_l, oos_s = oos_stats[key]["L"][0], oos_stats[key]["S"][0]
        if is_l > 0 and oos_l > 0:
            print(f"{key} L-side: IS={is_l:.1f} OOS={oos_l:.1f} -- CANDIDATE")
        if is_s > 0 and oos_s > 0:
            print(f"{key} S-side: IS={is_s:.1f} OOS={oos_s:.1f} -- CANDIDATE")


if __name__ == "__main__":
    main()
