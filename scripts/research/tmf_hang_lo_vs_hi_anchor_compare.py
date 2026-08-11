#!/usr/bin/env python3
"""2026-08-10: user wants entries to happen at a FIXED single line instead of
the current structure-based pick that sometimes lands near hang_lo (inner)
and sometimes near hang_hi (outer) depending on where recent price pivots
sit. Before touching src/tmf_channel/causal_engine.py's "frozen" birth_wants
cascade, compare two candidate anchors against the current live baseline:

  always_lo: structural pick forced to the INNER boundary (spot+/-hang_lo)
  always_hi: structural pick forced to the OUTER boundary (spot+/-hang_hi)

Monkeypatches _pick_hang_above/_pick_hang_below (module-level, not the
birth_wants closure) so the rest of birth_wants' existing cascade (tilt,
thermo dampen, contract-mode widening, cover-boost while in position) still
runs unchanged on top of whichever anchor this patch picks -- this tests
"should the structural anchor start near or far", not "bypass the whole
entry pipeline".

IMPORTANT mechanical note (state up front, don't bury): under a genuine
"both hang_lo and hang_hi rest as real orders, first touch wins" design,
the INNER (lo) line is reached before the OUTER (hi) line in the vast
majority of bars, since price has to pass through lo to reach hi on the
same side. So "dual real lines" and "always_lo" are expected to produce
very similar results -- this script's always_lo variant is the closest
faithful backtest proxy for that design without rebuilding dual-order
fill semantics into the engine.

Does NOT touch src/tmf_channel/causal_engine.py, src/order/*.py,
config/order.yaml, .env, launchd/, scripts/order/.
"""
from __future__ import annotations

import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

import tmf_channel.causal_engine as ce  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402

_ORIG_ABOVE = ce._pick_hang_above
_ORIG_BELOW = ce._pick_hang_below

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


def _patch_always_lo():
    def above(spot, levels, *, lo, hi, pad):
        return spot + lo
    def below(spot, levels, *, lo, hi, pad):
        return spot - lo
    ce._pick_hang_above = above
    ce._pick_hang_below = below


def _patch_always_hi():
    def above(spot, levels, *, lo, hi, pad):
        return spot + hi
    def below(spot, levels, *, lo, hi, pad):
        return spot - hi
    ce._pick_hang_above = above
    ce._pick_hang_below = below


def _restore():
    ce._pick_hang_above = _ORIG_ABOVE
    ce._pick_hang_below = _ORIG_BELOW


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


def run_day_net(arrays, gate, recipe_base, vix):
    O, H, L, C, V, T = arrays
    recipe = deepcopy(recipe_base)
    recipe["session_side_gate"] = gate
    trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    return round(sum(t["pnl"] for t in trades), 1), len(trades)


def paired_stats(deltas):
    n = len(deltas)
    if n < 2:
        return dict(n=n, mean=deltas[0] if deltas else 0.0, std=0.0, t=0.0, p=1.0)
    mean = st.mean(deltas)
    sd = st.stdev(deltas)
    t = 0.0 if sd == 0 else mean / (sd / (n ** 0.5))
    try:
        from scipy import stats as sp
        p = float(2 * (1 - sp.t.cdf(abs(t), df=n - 1)))
    except Exception:
        p = None
    return dict(n=n, mean=mean, std=sd, t=t, p=p)


def run_window(label, days, source_map, recipe_base, vix, variant_name, patch_fn):
    arrays_cache = {}
    gate_cache = {}
    for d in days:
        arr = load_arrays(d, source_map)
        if arr is None:
            continue
        arrays_cache[d] = arr
        gate_cache[d] = continuous_gate_for_day(d, arr[5], source=SOURCE_FOR_DAY.get(d, "tx_1m_fullnight_cache_full.json"))

    _restore()
    baseline_net = {}
    baseline_n = {}
    for d, arr in arrays_cache.items():
        net, n = run_day_net(arr, gate_cache[d], recipe_base, vix)
        baseline_net[d] = net
        baseline_n[d] = n

    patch_fn()
    try:
        variant_deltas = []
        variant_ns = []
        for d, arr in arrays_cache.items():
            net, n = run_day_net(arr, gate_cache[d], recipe_base, vix)
            variant_deltas.append(net - baseline_net[d])
            variant_ns.append(n)
    finally:
        _restore()

    stats = paired_stats(variant_deltas)
    total_trades = sum(variant_ns)
    baseline_total_trades = sum(baseline_n.values())
    i_max = max(range(len(variant_deltas)), key=lambda i: abs(variant_deltas[i])) if variant_deltas else None
    excl_mean = None
    if i_max is not None and len(variant_deltas) > 1:
        excl = variant_deltas[:i_max] + variant_deltas[i_max + 1:]
        excl_mean = st.mean(excl) if excl else None

    print(f"=== {label} · {variant_name} ({len(days)} days requested) ===")
    print(f"n_days={stats['n']} baseline_trades={baseline_total_trades} variant_trades={total_trades} "
          f"sum_delta={sum(variant_deltas):.1f} mean={stats['mean']:.2f} std={stats['std']:.2f} "
          f"t={stats['t']:.3f} p={stats['p']} excl_top_day_mean={excl_mean}")
    return dict(n=stats["n"], mean=stats["mean"], std=stats["std"], t=stats["t"], p=stats["p"],
                sum_delta=round(sum(variant_deltas), 1), baseline_trades=baseline_total_trades,
                variant_trades=total_trades, excl_top_day_mean=excl_mean)


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")
    recipe_base["session_pv_book"] = specialized_cell_book()

    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]

    results = {}
    for variant_name, patch_fn in (("always_lo", _patch_always_lo), ("always_hi", _patch_always_hi)):
        results[f"IS_{variant_name}"] = run_window(
            "IN-SAMPLE(22d)", IS_DAYS, SOURCE_FOR_DAY, recipe_base, vix, variant_name, patch_fn)
        results[f"OOS_{variant_name}"] = run_window(
            "OUT-OF-SAMPLE(66d)", oos_days,
            {d: "tx_1m_fullnight_cache_full.json" for d in oos_days},
            recipe_base, vix, variant_name, patch_fn)

    print("\n=== SUMMARY (delta vs current live structure-based baseline) ===")
    for k, r in results.items():
        print(f"{k:22s} n={r['n']:2d} mean={r['mean']:8.2f} t={r['t']:6.2f} p={r['p']} "
              f"sum_delta={r['sum_delta']:9.1f} trades(base->variant)={r['baseline_trades']}->{r['variant_trades']}")

    import json
    out_path = "reports/research/channel_lab/tmf_hang_lo_vs_hi_anchor_compare_result.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
