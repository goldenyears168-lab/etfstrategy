#!/usr/bin/env python3
"""One-off adversarial recheck (2026-08-12): gated (PV16) vs ungated
(day/night uniform "normal"-cell, PV8 collapsed) on the 3 most recent real
trading days, using the same order-layer-aware replay harness as
tmf_order_layer_aware_replay.py (imports the REAL production order-layer
functions, not a re-simulation of causal_engine.simulate() alone).

Mirrors tonight's earlier 30-day stratified batch's recipe construction
exactly:
  - gated   = unmodified order.tmf_channel_config.PAPER_RECIPE (16-cell
              session_pv_book, i.e. production default).
  - ungated = PAPER_RECIPE with session_pv_book replaced by a 2-cell book
              (day / night only): every one of the 8 PV8 regimes within a
              session maps to that session's *current* tuned "normal" cell
              recipe (CELL_TUNE_V2-patched, day|normal / night|normal).
              PV8 classification still runs (classify_pv is untouched) but
              its output no longer changes which recipe is looked up.

NQ 1m spread gate neutralized (mocked fail-open) identically to the earlier
batch: tmf_channel.nq_1m_spread_gate.spread_side_for_day /
last_spread_load_error / last_spread_debug all patched to return None,
because the 1m spread cache does not have historical coverage for these
dates and the gate is fail-closed-hard-block by default when None is NOT
returned incorrectly (i.e. we want it out of the comparison, same as
before -- both configs get the identical neutralization).

Does not touch src/order/ files, config/order.yaml, .env, launchd/,
scripts/order/, or any live ledger/session state -- only imports and
monkeypatches within this process.
"""
from __future__ import annotations

import json
import sys
from copy import deepcopy

sys.path.insert(0, "src")

import tmf_channel.nq_1m_spread_gate as _spread_gate_mod  # noqa: E402

_spread_gate_mod.spread_side_for_day = lambda *a, **k: None
_spread_gate_mod.last_spread_load_error = lambda: None
_spread_gate_mod.last_spread_debug = lambda: None

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import PV8, specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import load_day  # noqa: E402

sys.path.insert(0, "scripts/research")
from tmf_order_layer_aware_replay import (  # noqa: E402
    patch_nq_gate_for_backfill,
    replay_day,
)

patch_nq_gate_for_backfill(lookback_days=400)

DAYS = ["2026-07-16", "2026-07-28", "2026-08-07"]
SOURCE = "tx_1m_tick_built_582d"


def build_gated_recipe() -> dict:
    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")
    return recipe


def build_ungated_recipe() -> dict:
    """Same PAPER_RECIPE, but session_pv_book collapsed: day/night split kept,
    PV8 cell-selection removed -- every regime within a session uses that
    session's current tuned 'normal' cell params.
    """
    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")
    full_book = specialized_cell_book()
    day_normal = deepcopy(full_book["day"]["normal"])
    night_normal = deepcopy(full_book["night"]["normal"])
    uniform_book = {"day": {}, "night": {}}
    for pv in PV8:
        uniform_book["day"][pv] = deepcopy(day_normal)
        uniform_book["night"][pv] = deepcopy(night_normal)
    recipe["session_pv_book"] = uniform_book
    return recipe


def run_config(label: str, recipe: dict) -> dict:
    per_day = []
    total_trades = 0
    total_pnl = 0.0
    for day in DAYS:
        rows = load_day(day, source=SOURCE)
        if not rows:
            per_day.append({"day": day, "error": "no_rows"})
            continue
        result = replay_day(day, rows, recipe)
        per_day.append(
            {
                "day": day,
                "n_trades": result["n_trades"],
                "sum_pnl": result["sum_pnl"],
            }
        )
        total_trades += result["n_trades"]
        total_pnl += result["sum_pnl"]
        print(
            f"PROGRESS config={label} day={day} n_trades={result['n_trades']} "
            f"sum_pnl={result['sum_pnl']} cum_trades={total_trades} "
            f"cum_pnl={round(total_pnl, 1)}",
            file=sys.stderr,
            flush=True,
        )
    return {
        "config": label,
        "n_trades": total_trades,
        "sum_pnl": round(total_pnl, 1),
        "per_day": per_day,
    }


def main() -> None:
    gated = run_config("gated", build_gated_recipe())
    ungated = run_config("ungated", build_ungated_recipe())
    print("RESULT_JSON " + json.dumps({"gated": gated, "ungated": ungated}, ensure_ascii=False))


if __name__ == "__main__":
    main()
