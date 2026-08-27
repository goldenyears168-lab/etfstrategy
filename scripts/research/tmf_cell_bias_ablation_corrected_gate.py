#!/usr/bin/env python3
"""Clean ablation (2026-08-10): does cell.bias=True (the NQ-gate hard-block
-- "gate says none while flat -> block both sides") actually help, when
tested with a CORRECTLY-anchored gate instead of the look-ahead-biased one
that originally justified it?

Background: cell.bias=True was introduced 2026-08-06 as part of an
"architecture" refactor (config/strategy.yaml's arch_cut_20260806 entry,
graduation: architecture, not a backtest claim). Tracing it back further,
its FIRST appearance is v1.2.0's H2H graduation report
(r_h2h_v113_vs_specialized.json), whose own verdict note says "Stack
differs by design -- this is the replace-live question, not a
single-knob ablation" -- i.e. NQ-gate/bias was never isolated from the
16-cell book, EARLY fill, and VIX blend changes bundled in the same H2H
test. Worse: that report's own build_nq_gate() (r_strict_paper_bias_
overlay.py) anchors every day's gate value to that day's OWN night-session
open (15:00) when night bars exist, REGARDLESS of whether the decision
being informed is for the day session (which closed hours earlier that
same date) or the night session -- the exact same same-day look-ahead
bug later found and used to invalidate CELL_TUNE_V3's evidence
(r_gate_anchor_v4_audit.json), but never re-checked against v1.2.0's own
original bias/gate evidence.

This script tests bias=True vs bias=False (all 16 cells, all else
identical -- SPECIALIZED_PATCHES + CELL_TUNE_V2_PATCHES on both arms)
using the CORRECTED, non-look-ahead nq_gate.py (src/tmf_channel/nq_gate.py,
already fixed and live tonight, with the 60-day lookback patch so historical
--day values outside the live 10-day rolling window don't spuriously read
as "none"). One session_side_gate value per calendar day (anchored at
day-session open, 08:45 -- causal_engine.py's own ssg.get(day, "none")
lookup is inherently per-CALENDAR-DAY, not per-session, so this matches
the engine's actual granularity; it does NOT reproduce desired_from_
simulate()'s live behavior of recomputing per-poll with the current hm,
which is a live-implementation nuance, not what the engine itself
structurally supports).

Tested on the same 22-day in-sample + 66-day out-of-sample windows used
throughout tonight, day-clustered paired comparison.

Does NOT touch src/tmf_channel/causal_engine.py, src/order/, config/order.yaml,
.env, launchd/, scripts/order/.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_channel.nq_gate import nq_side_for_day  # noqa: E402
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


def bias_off_book(book: dict) -> dict:
    out = deepcopy(book)
    for sess in ("day", "night"):
        for reg, cell in out[sess].items():
            cell["bias"] = False
    return out


def run_day(day: str, recipe_bias_on: dict, recipe_bias_off: dict, vix: dict) -> dict:
    source = SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json")
    rows = load_day(day, source=source)
    if not rows:
        return dict(day=day, skipped=True)
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]

    side = nq_side_for_day(day, hm="08:45")
    gate = {day: side} if side is not None else {}
    r_on = dict(recipe_bias_on)
    r_on["session_side_gate"] = gate
    r_off = dict(recipe_bias_off)
    r_off["session_side_gate"] = gate

    trades_on, *_ = simulate(O, H, L, C, V, T, r_on, vix_delta=vix)
    trades_off, *_ = simulate(O, H, L, C, V, T, r_off, vix_delta=vix)
    net_on = round(sum(t["pnl"] for t in trades_on), 1)
    net_off = round(sum(t["pnl"] for t in trades_off), 1)

    return dict(
        day=day, nq_gate_side=side,
        n_bias_on=len(trades_on), net_bias_on=net_on,
        n_bias_off=len(trades_off), net_bias_off=net_off,
        diff=round(net_off - net_on, 1),
    )


def run_window(days, recipe_on, recipe_off, vix, label):
    rows = []
    for day in days:
        r = run_day(day, recipe_on, recipe_off, vix)
        if r.get("skipped"):
            continue
        rows.append(r)
        print(json.dumps(r), flush=True)

    diffs = [r["diff"] for r in rows]
    n = len(rows)
    mean_d = st.mean(diffs)
    std_d = st.stdev(diffs) if n > 1 else 0.0
    t_stat = mean_d / (std_d / (n ** 0.5)) if std_d > 0 else 0.0
    try:
        from scipy import stats as sp

        p_val = float(2 * (1 - sp.t.cdf(abs(t_stat), df=n - 1))) if n > 1 else None
    except Exception:
        p_val = None

    n_none_days = sum(1 for r in rows if r["nq_gate_side"] in (None, "none"))
    print(f"\n=== {label} summary ===")
    print(f"n={n} bias_on_sum={sum(r['net_bias_on'] for r in rows):.1f} "
          f"bias_off_sum={sum(r['net_bias_off'] for r in rows):.1f} "
          f"n_gate_none_or_unavailable={n_none_days}/{n}")
    print(f"diff(off-on) mean={mean_d:.2f} std={std_d:.2f} t={t_stat:.3f} p={p_val}")
    return dict(n=n, mean_diff=mean_d, std_diff=std_d, t=t_stat, p=p_val,
                n_gate_none=n_none_days, rows=rows)


def main():
    patch_nq_gate_for_backfill(lookback_days=60)
    vix = load_vixtwn_delta() or {}

    recipe_on = deepcopy(PAPER_RECIPE)
    recipe_on.setdefault("hang_anchor", "O")
    recipe_off = deepcopy(PAPER_RECIPE)
    recipe_off["session_pv_book"] = bias_off_book(PAPER_RECIPE["session_pv_book"])
    recipe_off.setdefault("hang_anchor", "O")

    is_days = JULY_DAYS + AUG_DAYS
    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]

    is_result = run_window(is_days, recipe_on, recipe_off, vix, "IN-SAMPLE (22 days)")
    oos_result = run_window(oos_days, recipe_on, recipe_off, vix, "OUT-OF-SAMPLE (66 days)")

    out_path = "reports/research/channel_lab/tmf_cell_bias_ablation_corrected_gate_result.json"
    with open(out_path, "w") as f:
        json.dump(dict(in_sample=is_result, out_of_sample=oos_result), f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
