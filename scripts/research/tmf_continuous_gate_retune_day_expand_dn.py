#!/usr/bin/env python3
"""Retune cell day|expand_dn against the NEW continuous NQ/ES gate (2026-08-10).

Context: causal_engine.py's session_side_gate lookup now supports a
per-bar-timestamp key (backward compatible) and nq_gate.py's
nq_side_for_day() has been swapped live from a frozen-at-session-open
anchor to a CONTINUOUS anchor. This is already deployed. Now that the
underlying regime-gate signal has changed, the day|expand_dn cell's own
params (tuned against the OLD frozen-gate behavior, see
order.tmf_channel_pv16_book CELL_TUNE_V2_PATCHES / SPECIALIZED_PATCHES --
neither touches day|expand_dn, so its live params are exactly
freeze_cell_book()'s day_base with DAY_BLOCKS["expand_dn"]=["L"]) may no
longer be optimal. This script re-tunes ONLY that one cell, holding all
other 15 cells at the current-live (specialized_cell_book()) default.

Methodology matches scripts/research/tmf_continuous_gate_vs_frozen_anchor.py
(reused directly, not rebuilt) for the continuous-gate construction, plus
scripts/research/tmf_order_layer_aware_replay.py's patch_nq_gate_for_backfill
for the NQ/ES historical-fetch widening. True re-simulation via
tmf_channel.engine.simulate() (causal, no order-layer discipline modeled --
same level as all sibling cell-tune work tonight), day-clustered paired
stats (candidate minus current-live baseline, same continuous gate on both
sides), in-sample search over 22 days, single best candidate validated OOS
over 66 days.

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

SESSION = "day"
PV = "expand_dn"

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

BASELINE_CELL = dict(specialized_cell_book()[SESSION][PV])
print(f"baseline current-live cell {SESSION}|{PV} = {BASELINE_CELL}", flush=True)

CANDIDATES: dict[str, dict] = {
    "current_default": {},
    "wider_15_35": {"hang_lo": 15.0, "hang_hi": 35.0},
    "wider_20_38": {"hang_lo": 20.0, "hang_hi": 38.0},
    "narrower_10_22": {"hang_lo": 10.0, "hang_hi": 22.0},
    "narrower_12_25": {"hang_lo": 12.0, "hang_hi": 25.0},
    "hold_16": {"max_hold_bars": 16},
    "hold_45": {"max_hold_bars": 45},
    "wider_hold16": {"hang_lo": 18.0, "hang_hi": 34.0, "max_hold_bars": 16},
    "gamma_0": {"early_fill_gamma": 0.0},
    "gamma_15": {"early_fill_gamma": 15.0},
    "block_both": {"block": ["L", "S"]},
}


def build_book(overrides: dict) -> dict:
    book = deepcopy(specialized_cell_book())
    cell = book[SESSION][PV]
    cell.update(overrides)
    return book


def day_trades_for_cell(day: str, book: dict, gate: dict, vix: dict) -> list[dict]:
    source = SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json")
    rows = load_day(day, source=source)
    if not rows:
        return []
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = bar_timestamps(day, rows, source=source)

    recipe = deepcopy(PAPER_RECIPE)
    recipe["session_pv_book"] = book
    recipe["session_side_gate"] = gate

    trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    out = []
    for t in trades:
        if t.get("regime_e") != PV:
            continue
        hm = str(t.get("et") or "")
        hm = hm.split("T", 1)[1][:5] if "T" in hm else hm[:5]
        sess = "day" if "08:45" <= hm < "13:45" else "night"
        if sess != SESSION:
            continue
        out.append(t)
    return out


def gate_and_bars_for_day(day: str, vix: dict):
    source = SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json")
    rows = load_day(day, source=source)
    if not rows:
        return None, None
    T = bar_timestamps(day, rows, source=source)
    gate = continuous_gate_for_day(day, T, source=SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json"))
    return gate, rows


def run_window(days: list[str], overrides: dict, vix: dict, label: str, gate_cache: dict):
    cand_book = build_book(overrides)
    base_book = deepcopy(specialized_cell_book())

    diffs = []
    rows_out = []
    n_appearances_base = 0
    for day in days:
        gate = gate_cache.get(day)
        if gate is None:
            source = SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json")
            rows = load_day(day, source=source)
            if not rows:
                continue
            T = bar_timestamps(day, rows, source=source)
            gate = continuous_gate_for_day(day, T, source=SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json"))
            gate_cache[day] = gate

        base_trades = day_trades_for_cell(day, base_book, gate, vix)
        cand_trades = day_trades_for_cell(day, cand_book, gate, vix)
        n_appearances_base += len(base_trades)
        net_base = sum(t["pnl"] for t in base_trades)
        net_cand = sum(t["pnl"] for t in cand_trades)
        diff = round(net_cand - net_base, 1)
        diffs.append(diff)
        rows_out.append(dict(day=day, n_base=len(base_trades), net_base=round(net_base, 1),
                              n_cand=len(cand_trades), net_cand=round(net_cand, 1), diff=diff))

    n = len(diffs)
    mean_d = st.mean(diffs) if n else 0.0
    std_d = st.stdev(diffs) if n > 1 else 0.0
    t_stat = mean_d / (std_d / (n ** 0.5)) if std_d > 0 and n > 1 else 0.0
    try:
        from scipy import stats as sp
        p_val = float(2 * (1 - sp.t.cdf(abs(t_stat), df=n - 1))) if n > 1 else None
    except Exception:
        p_val = None

    print(f"  [{label}] n={n} n_base_trade_appearances={n_appearances_base} "
          f"mean={mean_d:.2f} std={std_d:.2f} t={t_stat:.3f} p={p_val}", flush=True)
    return dict(n=n, mean=mean_d, std=std_d, t=t_stat, p=p_val, rows=rows_out,
                n_base_appearances=n_appearances_base)


def main():
    patch_nq_gate_for_backfill(lookback_days=60)
    vix = load_vixtwn_delta() or {}
    gate_cache: dict[str, dict] = {}

    print("=== IN-SAMPLE SEARCH (22 days) ===", flush=True)
    is_results = {}
    for name, overrides in CANDIDATES.items():
        print(f"candidate={name} overrides={overrides}", flush=True)
        res = run_window(IS_DAYS, overrides, vix, name, gate_cache)
        is_results[name] = res

    # Rank by day-clustered mean delta (excluding current_default, which is
    # the "no change" control = 0 by construction).
    ranked = sorted(
        ((k, v) for k, v in is_results.items() if k != "current_default"),
        key=lambda kv: kv[1]["mean"], reverse=True,
    )
    print("\n=== IN-SAMPLE RANKING (mean day-clustered delta vs current-live) ===")
    for name, res in ranked:
        print(f"  {name}: mean={res['mean']:.2f} std={res['std']:.2f} t={res['t']:.3f} "
              f"p={res['p']} n={res['n']}")

    best_name, best_res = ranked[0] if ranked else (None, None)

    out = dict(baseline_cell=BASELINE_CELL, candidates=CANDIDATES,
               in_sample=is_results, best_in_sample=best_name)

    if best_res is None or best_res["mean"] <= 0:
        print("\nNo candidate beats current default in-sample -> "
              "recommend keeping current default.")
        out["verdict_prelim"] = "NO_IMPROVEMENT"
    else:
        # Single-day-artifact check: exclude the largest-|delta| day.
        rows = best_res["rows"]
        if rows:
            max_row = max(rows, key=lambda r: abs(r["diff"]))
            rest = [r["diff"] for r in rows if r is not max_row]
            rest_mean = st.mean(rest) if rest else 0.0
            print(f"\nSingle-day-artifact check for best candidate '{best_name}': "
                  f"largest |delta| day={max_row['day']} diff={max_row['diff']}, "
                  f"mean excluding it={rest_mean:.2f} (full mean={best_res['mean']:.2f})")
            out["artifact_check"] = dict(max_day=max_row["day"], max_diff=max_row["diff"],
                                          mean_excl=rest_mean)

        print(f"\n=== OUT-OF-SAMPLE VALIDATION: candidate '{best_name}' ===")
        oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json")
                    if d < "2026-07-08"]
        oos_gate_cache: dict[str, dict] = {}
        oos_res = run_window(oos_days, CANDIDATES[best_name], vix, best_name, oos_gate_cache)
        out["out_of_sample"] = oos_res
        out["oos_days_n"] = len(oos_days)

        if oos_res["n"] and oos_res["mean"] > 0 and oos_res["p"] is not None and oos_res["p"] < 0.10:
            out["verdict_prelim"] = "ADOPT"
        elif oos_res["n"] and oos_res["mean"] <= 0:
            out["verdict_prelim"] = "OOS_FAILED"
        else:
            out["verdict_prelim"] = "NO_IMPROVEMENT"

    # NOTE: reports/research/channel_lab/ is read-only for this task -- write
    # scratch output elsewhere instead.
    out_path = "/tmp/tmf_continuous_gate_retune_day_expand_dn_result.json"
    try:
        with open(out_path, "w") as f:
            json.dump(out, f, indent=2, ensure_ascii=False, default=str)
        print(f"\nWrote {out_path}")
    except Exception as e:
        print(f"\n(could not write {out_path}: {e})")


if __name__ == "__main__":
    main()
