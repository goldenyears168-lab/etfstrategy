#!/usr/bin/env python3
"""Cell retune (2026-08-10): night|expand_dn under the NEWLY-LIVE continuous
(per-bar) NQ/ES session_side_gate (see scripts/research/
tmf_continuous_gate_vs_frozen_anchor.py -- already validated ADOPT on both
22-day in-sample and 66-day OOS windows, p=0.0037 OOS, ALREADY DEPLOYED
LIVE tonight).

Assigned cell: night|expand_dn ONLY. Every other one of the 16
session_pv_book cells stays at the current-live-equivalent default
(order.tmf_channel_pv16_book.specialized_cell_book()) for the entire run.

IMPORTANT baseline fact: in the CURRENT live book, night|expand_dn is
already block=["L","S"] (CELL_TUNE_V2_PATCHES; SPECIALIZED_PATCHES' own
hang_lo=16/hang_hi=30/max_hold_bars=16 tuning is moot while blocked) --
i.e. it fires ZERO trades today. So "current default" contributes a fixed
net-pnl-delta of 0 on every day by construction, and the whole question is
whether *unblocking* it (with tuned hang/hold params, now that the
underlying regime gate is continuous rather than frozen-at-open) can
extract a positive, day-clustered, OOS-surviving edge.

Does NOT touch src/tmf_channel/causal_engine.py, src/tmf_channel/nq_gate.py,
src/order/*.py, config/order.yaml, .env, launchd/, scripts/order/,
config/strategy.yaml, config/strategies.yaml. Only reads
reports/research/channel_lab/ (never writes there).
"""
from __future__ import annotations

import statistics as st
import sys
from copy import deepcopy
from math import erf, sqrt

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402

MY_SESS = "night"
MY_PV = "expand_dn"

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})
IN_SAMPLE_DAYS = JULY_DAYS + AUG_DAYS


def day_in_window(hm: str) -> bool:
    return "08:45" <= hm < "13:45"


def load_arrays(day: str, source_map: dict):
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


def run_book(arrays, gate: dict, book, recipe_base: dict, vix: dict):
    O, H, L, C, V, T = arrays
    recipe = deepcopy(recipe_base)
    recipe["session_pv_book"] = book
    recipe["session_side_gate"] = gate
    trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)

    net = 0.0
    n = 0
    for tr in trades:
        if tr.get("regime_e") != MY_PV:
            continue
        hm = str(tr.get("et") or "")
        hm = hm.split("T", 1)[1][:5] if "T" in hm else hm[:5]
        sess = "day" if day_in_window(hm) else "night"
        if sess != MY_SESS:
            continue
        net += float(tr["pnl"])
        n += 1
    return net, n


def build_book(overrides: dict | None):
    book = deepcopy(specialized_cell_book())
    if overrides:
        book[MY_SESS][MY_PV].update(overrides)
    return book


CANDIDATES = [
    ("baseline_current_default", None),  # block=["L","S"] -> always 0 trades
    ("unblock_same_band", dict(block=[])),
    ("unblock_wide_band", dict(block=[], hang_lo=22.0, hang_hi=40.0)),
    ("unblock_wide_band_hold30", dict(
        block=[], hang_lo=25.0, hang_hi=45.0, max_hold_bars=30)),
    ("unblock_narrow_band_hold12", dict(
        block=[], hang_lo=12.0, hang_hi=24.0, max_hold_bars=12)),
    ("short_only_same_band", dict(block=["L"])),
    ("short_only_wide_band_hold30", dict(
        block=["L"], hang_lo=25.0, hang_hi=45.0, max_hold_bars=30)),
    ("short_only_narrow_band_hold12", dict(
        block=["L"], hang_lo=12.0, hang_hi=24.0, max_hold_bars=12)),
    ("long_only_same_band", dict(block=["S"])),
    ("unblock_tight_eager", dict(
        block=[], hang_lo=18.0, hang_hi=32.0, max_hold_bars=20,
        early_fill_gamma=9.0)),
]


def paired_stats(deltas: list[float]):
    n = len(deltas)
    if n < 2:
        return dict(n=n, mean=deltas[0] if deltas else 0.0, std=0.0, t=0.0, p=1.0)
    mean = st.mean(deltas)
    sd = st.stdev(deltas)
    t = 0.0 if sd == 0 else mean / (sd / (n ** 0.5))
    p = 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2))))
    return dict(n=n, mean=mean, std=sd, t=t, p=p)


def evaluate(days: list[str], source_map: dict, recipe_base: dict, vix: dict):
    arrays_cache = {}
    gate_cache = {}
    for d in days:
        arr = load_arrays(d, source_map)
        if arr is None:
            continue
        arrays_cache[d] = arr
        gate_cache[d] = continuous_gate_for_day(d, arr[5], source=SOURCE_FOR_DAY.get(d, "tx_1m_fullnight_cache_full.json"))  # T is arr[5]

    baseline_book = build_book(None)
    baseline_day_net = {}
    baseline_day_n = {}
    for d, arr in arrays_cache.items():
        bnet, bn = run_book(arr, gate_cache[d], baseline_book, recipe_base, vix)
        baseline_day_net[d] = bnet
        baseline_day_n[d] = bn

    per_cand_day_delta = {}
    per_cand_day_n = {}
    for label, overrides in CANDIDATES:
        book = build_book(overrides)
        day_delta = {}
        day_n = {}
        for d, arr in arrays_cache.items():
            cnet, cn = run_book(arr, gate_cache[d], book, recipe_base, vix)
            day_delta[d] = cnet - baseline_day_net[d]
            day_n[d] = cn
        per_cand_day_delta[label] = day_delta
        per_cand_day_n[label] = day_n

    return per_cand_day_delta, per_cand_day_n, baseline_day_n


def main():
    patch_nq_gate_for_backfill(lookback_days=60)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")

    print(f"=== IN-SAMPLE ({len(IN_SAMPLE_DAYS)} days) ===")
    per_cand_delta, per_cand_n, baseline_net = evaluate(
        IN_SAMPLE_DAYS, SOURCE_FOR_DAY, recipe_base, vix)
    baseline_n_vals = sorted(set(per_cand_n["baseline_current_default"].values()))
    print(f"baseline night|expand_dn trade counts (should be all 0): {baseline_n_vals}")

    summary = {}
    for label, _ov in CANDIDATES:
        deltas = list(per_cand_delta[label].values())
        trade_ns = list(per_cand_n[label].values())
        total_trades = sum(trade_ns)
        stats = paired_stats(deltas)
        if len(deltas) >= 2:
            i_max = max(range(len(deltas)), key=lambda i: abs(deltas[i]))
            excl = deltas[:i_max] + deltas[i_max + 1:]
            excl_mean = st.mean(excl) if excl else 0.0
        else:
            excl_mean = None
        summary[label] = dict(stats=stats, total_trades=total_trades,
                               excl_top_day_mean=excl_mean, sum_delta=sum(deltas))
        print(f"{label:28s} n_days={stats['n']:2d} total_trades={total_trades:4d} "
              f"sum_delta={sum(deltas):8.1f} mean={stats['mean']:7.2f} "
              f"std={stats['std']:7.2f} t={stats['t']:6.2f} p={stats['p']:.3f} "
              f"excl_top_day_mean={excl_mean}")

    print("\n=== picking best in-sample candidate (excluding baseline) ===")
    candidates_only = [(l, s) for l, s in summary.items() if l != "baseline_current_default"]
    thin = all(s["total_trades"] < 15 for _, s in candidates_only)
    best_label = None
    if thin:
        print("ALL candidates thin (<15 trades across 22 days) -> INSUFFICIENT_DATA")
    else:
        eligible = [(l, s) for l, s in candidates_only if s["total_trades"] >= 15]
        eligible.sort(key=lambda kv: kv[1]["stats"]["mean"], reverse=True)
        best_label, best_summary = eligible[0]
        print(f"best candidate: {best_label} -> {summary[best_label]}")
        if best_summary["stats"]["mean"] <= 0:
            print("best candidate's mean delta <= 0 -> current default (blocked) wins")
            best_label = None

    if best_label is None:
        print("\nFINAL VERDICT: NO_IMPROVEMENT (or INSUFFICIENT_DATA, see above) "
              "-- keep current default (block=['L','S']) for night|expand_dn")
        return

    best_overrides = dict(CANDIDATES)[best_label]
    print(f"\n=== OOS validation of {best_label} ({best_overrides}) ===")
    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json")
                if d < "2026-07-08"]
    print(f"OOS day count: {len(oos_days)}")
    oos_source_map = {d: "tx_1m_fullnight_cache_full.json" for d in oos_days}

    baseline_book = build_book(None)
    cand_book = build_book(best_overrides)
    oos_deltas = []
    oos_ns = []
    for d in oos_days:
        arr = load_arrays(d, oos_source_map)
        if arr is None:
            continue
        gate = continuous_gate_for_day(d, arr[5], source=SOURCE_FOR_DAY.get(d, "tx_1m_fullnight_cache_full.json"))
        bnet, _ = run_book(arr, gate, baseline_book, recipe_base, vix)
        cnet, cn = run_book(arr, gate, cand_book, recipe_base, vix)
        oos_deltas.append(cnet - bnet)
        oos_ns.append(cn)

    oos_stats = paired_stats(oos_deltas)
    print(f"OOS: n_days={oos_stats['n']} total_trades={sum(oos_ns)} "
          f"sum_delta={sum(oos_deltas):.1f} mean={oos_stats['mean']:.2f} "
          f"std={oos_stats['std']:.2f} t={oos_stats['t']:.2f} p={oos_stats['p']:.3f}")

    print("\n=== FINAL ===")
    print(f"cell=night|expand_dn candidate={best_label} overrides={best_overrides}")
    print(f"IN-SAMPLE: {summary[best_label]['stats']} total_trades={summary[best_label]['total_trades']}")
    print(f"OOS: {oos_stats} total_trades={sum(oos_ns)}")
    if oos_stats["mean"] <= 0 or sum(oos_ns) < 15:
        print("OOS FAILS to confirm in-sample edge -> OOS_FAILED, keep current default")
    else:
        print("OOS confirms in-sample edge -> ADOPT candidate")


if __name__ == "__main__":
    main()
