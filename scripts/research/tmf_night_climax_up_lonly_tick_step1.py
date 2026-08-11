#!/usr/bin/env python3
"""Step 1 (tick-validation task): regenerate the exact set of night|climax_up
L-side trades produced by the 1m-bar simulate() under the candidate book
(night.climax_up block override -> ["S"], everything else unchanged) across
IS_DAYS (22d) + OOS_66d (66d). Dumps trades to JSON for the tick-replay step.
"""
from __future__ import annotations

import json
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


def build_candidate_book():
    book = deepcopy(specialized_cell_book())
    book["night"]["climax_up"]["block"] = ["S"]
    return book


def load_arrays(day, source):
    rows = load_day(day, source=source)
    if not rows:
        return None
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    # Real instants, NOT f"{day}T{t}". For a "session"-convention source
    # (tx_1m_fullnight_cache*) the 00:00-04:59 tail is calendar day+1, so the
    # naive form is 24h early and every downstream alignment against an external
    # time axis (NQ/ES gate, raw ticks) lands on the wrong day.
    # bar_timestamps() prefers each row's `cal` and raises for an unregistered
    # source rather than guessing.
    T = bar_timestamps(day, rows, source=source)
    return O, H, L, C, V, T


def is_night(et: str) -> bool:
    hm = et.split("T", 1)[1][:5] if "T" in et else et[:5]
    return hm >= "15:00" or hm < "05:00"


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")
    cand_book = build_candidate_book()

    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]

    windows = {
        "IS_22d": [(d, SOURCE_FOR_DAY.get(d, "tx_1m_fullnight_cache_full.json")) for d in IS_DAYS],
        "OOS_66d": [(d, "tx_1m_fullnight_cache_full.json") for d in oos_days],
    }

    all_trades = {}
    for label, day_srcs in windows.items():
        found = []
        for day, source in day_srcs:
            arr = load_arrays(day, source)
            if arr is None:
                continue
            O, H, L, C, V, T = arr
            gate = continuous_gate_for_day(day, T, source=source)
            recipe = deepcopy(recipe_base)
            recipe["session_side_gate"] = gate
            recipe["session_pv_book"] = cand_book
            trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
            for t in trades:
                if t.get("s") != "L":
                    continue
                if t.get("regime_e") != "climax_up":
                    continue
                if not is_night(str(t.get("et") or "")):
                    continue
                rec = dict(day=day, source=source, **t)
                found.append(rec)
        all_trades[label] = found
        net = sum(t["pnl"] for t in found)
        print(f"{label}: {len(found)} night|climax_up L trades, bar-sim net={net:.1f}pt")

    out_path = "reports/research/channel_lab/tmf_night_climax_up_lonly_trades_for_tick_check.json"
    import os
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_trades, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
