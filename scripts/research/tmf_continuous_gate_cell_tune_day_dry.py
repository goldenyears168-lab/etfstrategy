#!/usr/bin/env python3
"""Cell-tune (2026-08-10): day|dry, retuned under the NEW continuous NQ/ES
session_side_gate (already deployed live tonight -- see
scripts/research/tmf_continuous_gate_vs_frozen_anchor.py, which proved the
continuous gate beats the old frozen-at-session-open gate both in-sample
(22d) and OOS (66d, p=0.0037)).

ASSIGNED CELL: day|dry only. All other 15 cells stay at the current
live-equivalent default (order.tmf_channel_pv16_book.specialized_cell_book(),
i.e. SPECIALIZED_PATCHES + CELL_TUNE_V2_PATCHES on freeze_cell_book()) for
every run in this script -- only day|dry's hang_lo/hang_hi/max_hold_bars/
early_fill_gamma/block are varied.

Both candidate and baseline runs in every comparison use the SAME continuous
session_side_gate (built once per day via continuous_gate_for_day(), reused
across every candidate on that day -- it does not depend on session_pv_book)
so the comparison isolates day|dry's own params under the new gate regime,
not the gate change itself (that question is already answered by
tmf_continuous_gate_vs_frozen_anchor.py).

Does NOT touch src/order/*.py, src/tmf_channel/causal_engine.py,
src/tmf_channel/nq_gate.py, config/order.yaml, .env, launchd/,
scripts/order/, config/strategy.yaml, config/strategies.yaml.
"""
from __future__ import annotations

import json
import statistics as st
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
IN_SAMPLE_DAYS = JULY_DAYS + AUG_DAYS
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})

OOS_DAYS = [
    d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"
]
for d in OOS_DAYS:
    SOURCE_FOR_DAY[d] = "tx_1m_fullnight_cache_full.json"

MY_SESS = "day"
MY_PV = "dry"

BASE_CELL = dict(specialized_cell_book()["day"]["dry"])
# = {hang_lo: 13.0, hang_hi: 28.0, early_fill_gamma: 8.0, max_hold_bars: 30,
#    block: [], skip_quiet_mode: "dry", bias: True, vixtwn_calib: "blend",
#    vixtwn_calib_gamma: 5.0}

CANDIDATES: dict[str, dict] = {
    "current_default": {},
    # Continuous gate can now steer/block this cell's side mid-session
    # (it never could before, frozen anchor) -- surviving trades may behave
    # differently, so both wider and narrower bands are worth testing, not
    # just re-confirming the old tight 13-28 band.
    "narrow": dict(hang_lo=9.0, hang_hi=22.0),
    "wide": dict(hang_lo=18.0, hang_hi=34.0),
    "wider_still": dict(hang_lo=22.0, hang_hi=40.0),
    "wide_shorthold": dict(hang_lo=18.0, hang_hi=34.0, max_hold_bars=18),
    "wide_longhold": dict(hang_lo=18.0, hang_hi=34.0, max_hold_bars=42),
    "current_shorthold": dict(max_hold_bars=18),
    "current_longhold": dict(max_hold_bars=42),
    "current_gamma_up": dict(early_fill_gamma=14.0),
    "current_gamma0": dict(early_fill_gamma=0.0),
    "blocked": dict(block=["L", "S"]),
}


def _book_with(cell_override: dict) -> dict:
    book = deepcopy(specialized_cell_book())
    book[MY_SESS][MY_PV] = {**BASE_CELL, **cell_override}
    return book


def run_day(day: str, book: dict, recipe_base: dict, vix: dict, gate: dict) -> dict:
    source = SOURCE_FOR_DAY[day]
    rows = load_day(day, source=source)
    if not rows:
        return dict(day=day, n_trades=0, my_net=0.0, my_n=0, skipped=True)
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = bar_timestamps(day, rows, source=source)

    recipe = dict(recipe_base)
    recipe["session_pv_book"] = book
    recipe["session_side_gate"] = gate
    trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)

    my_trades = []
    for tr in trades:
        if str(tr.get("regime_e") or "") != MY_PV:
            continue
        hm = str(tr.get("et") or "")[11:16]
        sess = "day" if "08:45" <= hm < "13:45" else "night"
        if sess != MY_SESS:
            continue
        my_trades.append(tr)

    return dict(
        day=day,
        n_trades=len(trades),
        my_n=len(my_trades),
        my_net=round(sum(float(t["pnl"]) for t in my_trades), 1),
    )


def day_clustered_stats(deltas: list[float]) -> dict:
    n = len(deltas)
    if n == 0:
        return dict(n=0)
    mean = st.mean(deltas)
    sd = st.stdev(deltas) if n > 1 else 0.0
    t = (mean / (sd / (n ** 0.5))) if sd > 0 else (float("inf") if mean != 0 else 0.0)
    from math import erf, sqrt
    p = 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2)))) if sd > 0 else (0.0 if mean != 0 else 1.0)
    return dict(n=n, mean=round(mean, 2), std=round(sd, 2), t=round(t, 3), p=round(p, 4))


def evaluate(days: list[str], cell_override: dict, recipe_base: dict, vix: dict,
             baseline_cache: dict, gates: dict) -> dict:
    """Returns per-day my_net for candidate, plus deltas vs baseline_cache."""
    cand_book = _book_with(cell_override)
    deltas = []
    my_ns = []
    per_day = []
    for day in days:
        r = run_day(day, cand_book, recipe_base, vix, gates[day])
        base = baseline_cache[day]
        delta = r["my_net"] - base["my_net"]
        deltas.append(delta)
        my_ns.append(r["my_n"])
        per_day.append(dict(day=day, cand_net=r["my_net"], base_net=base["my_net"], delta=delta,
                             cand_n=r["my_n"], base_n=base["my_n"]))
    return dict(per_day=per_day, deltas=deltas, my_ns=my_ns,
                stats=day_clustered_stats(deltas), total_appearances=sum(my_ns))


def build_gates(days: list[str]) -> dict:
    gates = {}
    for day in days:
        source = SOURCE_FOR_DAY[day]
        rows = load_day(day, source=source)
        if not rows:
            gates[day] = {}
            continue
        T = bar_timestamps(day, rows, source=source)
        gates[day] = continuous_gate_for_day(day, T, source=SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json"))
    return gates


def main():
    patch_nq_gate_for_backfill(lookback_days=60)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")

    # 1. Build the continuous gate ONCE per in-sample day (reused across
    # every candidate).
    is_gates = build_gates(IN_SAMPLE_DAYS)

    # 2. Baseline (current_default cell params) for every in-sample day, once.
    baseline_book = _book_with({})
    baseline_cache = {}
    for day in IN_SAMPLE_DAYS:
        baseline_cache[day] = run_day(day, baseline_book, recipe_base, vix, is_gates[day])
        print("[baseline]", json.dumps(baseline_cache[day], ensure_ascii=False), flush=True)

    base_total_n = sum(baseline_cache[d]["my_n"] for d in IN_SAMPLE_DAYS)
    base_total_net = sum(baseline_cache[d]["my_net"] for d in IN_SAMPLE_DAYS)
    print(f"\n[baseline] total day|dry appearances={base_total_n}, net={base_total_net:.1f} "
          f"over {len(IN_SAMPLE_DAYS)} days")

    if base_total_n < 15:
        print("\n=== INSUFFICIENT DATA: fewer than ~15 day|dry appearances in-sample ===")
        out = dict(
            cell="day|dry", base_cell=BASE_CELL,
            in_sample_baseline=dict(n=base_total_n, net=base_total_net),
            verdict="INSUFFICIENT_DATA",
        )
        print("\n=== summary ===")
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return

    # 3. Sweep candidates (excluding current_default, already = baseline).
    results = {}
    for name, override in CANDIDATES.items():
        if name == "current_default":
            continue
        res = evaluate(IN_SAMPLE_DAYS, override, recipe_base, vix, baseline_cache, is_gates)
        results[name] = res
        s = res["stats"]
        print(f"\n[{name}] override={override}")
        print(f"  in-sample: n_days={s.get('n')} mean_delta={s.get('mean')} std={s.get('std')} "
              f"t={s.get('t')} p={s.get('p')} total_appearances={res['total_appearances']}")

    # 4. Pick best candidate by mean day-clustered delta (only if beats 0 and
    # has enough appearances); else "current_default" wins.
    best_name = "current_default"
    best_mean = 0.0
    for name, res in results.items():
        if res["total_appearances"] < 15:
            continue
        m = res["stats"].get("mean", 0.0)
        if m > best_mean:
            best_mean = m
            best_name = name

    print(f"\n=== BEST IN-SAMPLE CANDIDATE: {best_name} (mean_delta={best_mean:.2f}) ===")

    # Red-flag check: does excluding the single largest-|delta| day flip sign?
    excl_flip = False
    if best_name != "current_default":
        deltas = results[best_name]["deltas"]
        idx_max = max(range(len(deltas)), key=lambda i: abs(deltas[i]))
        excl = deltas[:idx_max] + deltas[idx_max + 1:]
        excl_mean = st.mean(excl) if excl else 0.0
        excl_flip = (excl_mean > 0) != (best_mean > 0)
        print(f"  excluding largest-|delta| day ({IN_SAMPLE_DAYS[idx_max]}, "
              f"delta={deltas[idx_max]:.1f}): mean_delta={excl_mean:.2f} "
              f"(sign {'FLIPS' if excl_flip else 'holds'})")

    # 5. OOS validation of the single best candidate only.
    oos_result = None
    if best_name != "current_default":
        oos_gates = build_gates(OOS_DAYS)
        baseline_book_oos = baseline_book  # same book object, session_pv_book identical
        baseline_cache_oos = {}
        for day in OOS_DAYS:
            baseline_cache_oos[day] = run_day(day, baseline_book_oos, recipe_base, vix, oos_gates[day])
        oos_res = evaluate(OOS_DAYS, CANDIDATES[best_name], recipe_base, vix,
                            baseline_cache_oos, oos_gates)
        oos_result = oos_res
        s = oos_res["stats"]
        print(f"\n[OOS validate {best_name}] n_days={s.get('n')} mean_delta={s.get('mean')} "
              f"std={s.get('std')} t={s.get('t')} p={s.get('p')} "
              f"total_appearances={oos_res['total_appearances']}")

    # 6. Verdict.
    if best_name == "current_default":
        verdict = "NO_IMPROVEMENT"
    else:
        s_oos = oos_result["stats"] if oos_result else {}
        if oos_result and s_oos.get("n", 0) > 0 and s_oos.get("mean", 0) > 0 and (s_oos.get("p") is None or s_oos.get("p") < 0.10):
            verdict = "ADOPT"
        else:
            verdict = "OOS_FAILED"

    out = dict(
        cell="day|dry",
        base_cell=BASE_CELL,
        in_sample_baseline=dict(n=base_total_n, net=base_total_net),
        in_sample_candidates={k: v["stats"] | dict(total_appearances=v["total_appearances"])
                               for k, v in results.items()},
        best_name=best_name,
        best_override=CANDIDATES.get(best_name, {}),
        largest_delta_day_flip=excl_flip,
        oos=oos_result["stats"] if oos_result else None,
        oos_total_appearances=oos_result["total_appearances"] if oos_result else None,
        verdict=verdict,
    )
    # Note: reports/research/channel_lab/ is read-only for this task -- print
    # the summary dict instead of writing an artifact there.
    print("\n=== summary ===")
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
