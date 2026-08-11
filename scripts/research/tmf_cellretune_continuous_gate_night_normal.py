#!/usr/bin/env python3
"""Retune night|normal cell under the NEW continuous (per-bar) NQ session
gate (2026-08-10), now already live in causal_engine.py + nq_gate.py.

Baseline = current-live 16-cell book (order.tmf_channel_pv16_book.
specialized_cell_book()), fed the CONTINUOUS session_side_gate (built once
per day via continuous_gate_for_day(), reused across all candidates for
that day -- it does not depend on session_pv_book). All 15 OTHER cells
stay at the current-live default throughout; only night|normal's
hang_lo/hang_hi/max_hold_bars/early_fill_gamma/block are swept.

IMPORTANT: the current-live night|normal cell is fully BLOCKED
(block=["L","S"], via CELL_TUNE_V2_PATCHES) -- it trades zero contracts
today, always. So the baseline net pnl for this cell is 0.0 on every day,
and every "delta" below IS simply the candidate's own net pnl (since there
is nothing to subtract). The sweep below is really asking: does the
continuous gate's steering make it safe/profitable to UNBLOCK night|normal
now, and if so at what hang band / hold / gamma?

Methodology: day-clustered paired comparison (candidate net pnl for
night|normal trades minus baseline net pnl for the SAME cell on the SAME
day, same continuous gate on both sides), across the 22-day in-sample
window, then OOS validation of the single winning candidate on the 66-day
out-of-sample window (2026-04-01..07-07).

Does NOT touch src/tmf_channel/causal_engine.py, src/tmf_channel/nq_gate.py,
src/order/, config/order.yaml, config/strategy.yaml, config/strategies.yaml,
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
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402

from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402

SESS = "night"
PV = "normal"

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

_OOS_ALL = list_days(source="tx_1m_fullnight_cache_full.json")
OOS_DAYS = [d for d in _OOS_ALL if d < "2026-07-08"]
for d in OOS_DAYS:
    SOURCE_FOR_DAY[d] = "tx_1m_fullnight_cache_full.json"

DAY_WIN = ("08:45", "13:45")


def is_day_session(et: str) -> bool:
    hm = et[11:16]
    return DAY_WIN[0] <= hm < DAY_WIN[1]


def base_book() -> dict:
    return specialized_cell_book()


def make_recipe(cell_overrides: dict | None, gate: dict) -> dict:
    recipe = deepcopy(PAPER_RECIPE)
    book = base_book()
    if cell_overrides is not None:
        book[SESS][PV] = {**book[SESS][PV], **cell_overrides}
    recipe["session_pv_book"] = book
    recipe["session_side_gate"] = gate
    recipe.setdefault("hang_anchor", "O")
    return recipe


def load_arrays(day: str):
    source = SOURCE_FOR_DAY[day]
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


def run_day_cell(day: str, recipe: dict, vix: dict, arrays, gate: dict) -> tuple[int, float]:
    """Return (n_trades, net_pnl) for THIS cell's (SESS, PV) trades only."""
    if arrays is None:
        return 0, 0.0
    O, H, L, C, V, T = arrays
    trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    n = 0
    net = 0.0
    for tr in trades:
        if tr.get("regime_e") != PV:
            continue
        if is_day_session(tr["et"]) != (SESS == "day"):
            continue
        n += 1
        net += float(tr["pnl"])
    return n, net


def paired_stats(deltas: list[float]):
    n = len(deltas)
    if n < 2:
        return dict(n=n, mean=(deltas[0] if n else 0.0), std=0.0, t=None, p=None)
    mean = st.mean(deltas)
    std = st.stdev(deltas)
    if std == 0:
        t = float("inf") if mean != 0 else 0.0
    else:
        t = mean / (std / (n ** 0.5))
    try:
        from scipy import stats as sstats

        p = float(2 * (1 - sstats.t.cdf(abs(t), df=n - 1))) if std else (0.0 if mean else 1.0)
    except Exception:
        import math

        p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / (2 ** 0.5)))) if std else (0.0 if mean else 1.0)
    return dict(n=n, mean=mean, std=std, t=t, p=p)


def evaluate_candidate(
    days: list[str], candidate: dict | None, baseline_cache: dict, vix: dict,
    arrays_cache: dict, gate_cache: dict,
):
    """Return (deltas, cand_ns, base_ns, per_day) -- candidate net minus
    baseline net, per day, both fed the SAME continuous gate for that day."""
    deltas = []
    cand_ns = []
    base_ns = []
    per_day = []
    for day in days:
        arrays = arrays_cache.get(day)
        if arrays is None:
            arrays = load_arrays(day)
            arrays_cache[day] = arrays
        if arrays is None:
            continue
        gate = gate_cache.get(day)
        if gate is None:
            T = arrays[5]
            gate = continuous_gate_for_day(day, T, source=SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json"))
            gate_cache[day] = gate
        if day not in baseline_cache:
            baseline_cache[day] = run_day_cell(day, make_recipe(None, gate), vix, arrays, gate)
        base_n, base_net = baseline_cache[day]
        cand_recipe = make_recipe(candidate, gate)
        cand_n, cand_net = run_day_cell(day, cand_recipe, vix, arrays, gate)
        deltas.append(cand_net - base_net)
        cand_ns.append(cand_n)
        base_ns.append(base_n)
        per_day.append(dict(
            day=day, base_n=base_n, base_net=base_net, cand_n=cand_n, cand_net=cand_net,
            delta=round(cand_net - base_net, 1),
        ))
    return deltas, cand_ns, base_ns, per_day


def main():
    patch_nq_gate_for_backfill(lookback_days=60)
    vix = load_vixtwn_delta() or {}
    baseline_cache: dict = {}
    arrays_cache: dict = {}
    gate_cache: dict = {}

    print(f"=== Baseline (current-live) cell {SESS}|{PV} ===")
    print(json.dumps(base_book()[SESS][PV], default=str))
    print("NOTE: current-live night|normal is block=['L','S'] -- baseline "
          "net pnl is 0.0 on every day by construction. Every candidate's "
          "'delta' below equals that candidate's own net pnl.")

    total_base_n = 0
    for day in IN_SAMPLE_DAYS:
        arrays = load_arrays(day)
        arrays_cache[day] = arrays
        if arrays is None:
            continue
        T = arrays[5]
        gate = continuous_gate_for_day(day, T, source=SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json"))
        gate_cache[day] = gate
        n, net = run_day_cell(day, make_recipe(None, gate), vix, arrays, gate)
        baseline_cache[day] = (n, net)
        total_base_n += n
    print(f"\nbaseline in-sample n_trades total for {SESS}|{PV} (continuous gate) = "
          f"{total_base_n} across {len(IN_SAMPLE_DAYS)} days (expect 0, blocked)")

    candidates = [
        ("unblock_current_18_32_g9_h20",
         dict(block=[], hang_lo=18.0, hang_hi=32.0, early_fill_gamma=9.0, max_hold_bars=20)),
        ("unblock_narrow_14_26", dict(block=[], hang_lo=14.0, hang_hi=26.0)),
        ("unblock_wide_22_38", dict(block=[], hang_lo=22.0, hang_hi=38.0)),
        ("unblock_wide_25_45", dict(block=[], hang_lo=25.0, hang_hi=45.0)),
        ("unblock_hold_short_12", dict(block=[], max_hold_bars=12)),
        ("unblock_hold_long_30", dict(block=[], max_hold_bars=30)),
        ("unblock_gamma_lo_4", dict(block=[], early_fill_gamma=4.0)),
        ("unblock_gamma_hi_14", dict(block=[], early_fill_gamma=14.0)),
        ("unblock_wide_hold_long", dict(block=[], hang_lo=22.0, hang_hi=38.0, max_hold_bars=30)),
        ("stay_blocked", dict(block=["L", "S"])),  # sanity == baseline, delta should == 0
    ]

    results = {}
    max_trades = 0
    for name, patch in candidates:
        deltas, cand_ns, base_ns, per_day = evaluate_candidate(
            IN_SAMPLE_DAYS, patch, baseline_cache, vix, arrays_cache, gate_cache
        )
        stats = paired_stats(deltas)
        results[name] = dict(
            patch=patch, stats=stats, per_day=per_day,
            sum_cand_n=sum(cand_ns), sum_base_n=sum(base_ns),
        )
        max_trades = max(max_trades, sum(cand_ns))
        print(f"\n--- candidate {name} {patch} ---")
        if stats["n"] >= 2:
            print(f"n_days={stats['n']} mean_delta={stats['mean']:.1f} std={stats['std']:.1f} "
                  f"t={stats['t']:.2f} p={stats['p']:.3f}")
        else:
            print(stats)
        print(f"sum_cand_n={sum(cand_ns)} sum_base_n={sum(base_ns)}")
        sorted_days = sorted(per_day, key=lambda r: abs(r["delta"]), reverse=True)
        if sorted_days:
            top = sorted_days[0]
            rest_mean = (
                (sum(r["delta"] for r in per_day) - top["delta"]) / max(1, len(per_day) - 1)
            )
            print(f"largest-|delta| day: {top['day']} delta={top['delta']} ; "
                  f"mean-excl-that-day={rest_mean:.1f} (full mean={stats['mean']:.1f})")

    out_path = "/tmp/tmf_cellretune_continuous_gate_night_normal_insample.json"
    with open(out_path, "w") as f:
        json.dump(
            {k: {kk: vv for kk, vv in v.items() if kk != "per_day"} for k, v in results.items()},
            f, indent=2, default=str,
        )
    print(f"\nWrote {out_path}")

    if max_trades < 15:
        print(f"\nTHIN SAMPLE: max candidate trade count across in-sample = {max_trades} "
              "(<15). Reporting INSUFFICIENT_DATA -- not forcing an unblock recommendation.")
        return

    # Rank unblocked candidates only (exclude the stay_blocked sanity check
    # from "best" selection -- it's a 0.0 restatement of baseline).
    unblocked_results = {k: v for k, v in results.items() if k != "stay_blocked"}
    ranked = sorted(unblocked_results.items(), key=lambda kv: kv[1]["stats"]["mean"], reverse=True)
    best_name, best = ranked[0]
    print(f"\n=== Best in-sample candidate: {best_name} mean_delta={best['stats']['mean']:.2f} "
          f"t={best['stats']['t']} p={best['stats']['p']} n_trades={best['sum_cand_n']} ===")

    if best["stats"]["mean"] <= 0:
        print("No unblocked candidate beats the blocked baseline in-sample. "
              "FINAL VERDICT: NO_IMPROVEMENT (keep current default = fully blocked).")
        return

    print(f"\n=== OOS validation ({len(OOS_DAYS)} days) of candidate {best_name} {best['patch']} ===")
    oos_baseline_cache: dict = {}
    oos_arrays_cache: dict = {}
    oos_gate_cache: dict = {}
    deltas, cand_ns, base_ns, per_day = evaluate_candidate(
        OOS_DAYS, best["patch"], oos_baseline_cache, vix, oos_arrays_cache, oos_gate_cache
    )
    oos_stats = paired_stats(deltas)
    if oos_stats["n"] >= 2:
        print(f"OOS n_days={oos_stats['n']} mean_delta={oos_stats['mean']:.2f} "
              f"std={oos_stats['std']:.2f} t={oos_stats['t']:.3f} p={oos_stats['p']}")
    else:
        print(f"OOS stats: {oos_stats}")
    print(f"OOS sum_cand_n={sum(cand_ns)} sum_base_n={sum(base_ns)}")
    sorted_days = sorted(per_day, key=lambda r: abs(r["delta"]), reverse=True)
    if sorted_days:
        top = sorted_days[0]
        rest_mean = (sum(r["delta"] for r in per_day) - top["delta"]) / max(1, len(per_day) - 1)
        print(f"OOS largest-|delta| day: {top['day']} delta={top['delta']} ; "
              f"mean-excl-that-day={rest_mean:.2f} (full mean={oos_stats['mean']:.2f})")

    out_path2 = "/tmp/tmf_cellretune_continuous_gate_night_normal_oos.json"
    with open(out_path2, "w") as f:
        json.dump(
            dict(candidate=best_name, patch=best["patch"], in_sample_stats=best["stats"],
                 oos_stats=oos_stats, oos_per_day=per_day),
            f, indent=2, default=str,
        )
    print(f"\nWrote {out_path2}")


if __name__ == "__main__":
    main()
