#!/usr/bin/env python3
"""2026-08-10: trade-feature scan under always_lo found struct_break exits
dominate the worst decile (near-100% of it) while opp_cover/trail dominate
the best decile. Test whether disabling struct_break exits entirely
(recipe['struct_disabled']=True, an existing param) improves always_lo --
this combination (struct-disabled UNDER always_lo specifically) has not
been tested before; struct_break itself has a long rejection history
against the smart-pick baseline (see memory tmf-structbreak-campaign,
~120 variants, all failed) so this is a check for a NEW base, not a
re-run of old ground.

Reports day-clustered paired stats: always_lo (struct enabled, current
leading candidate) vs always_lo+struct_disabled.
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


def _patch_always_lo():
    ce._pick_hang_above = lambda spot, levels, *, lo, hi, pad: spot + lo
    ce._pick_hang_below = lambda spot, levels, *, lo, hi, pad: spot - lo


def _restore():
    ce._pick_hang_above = _ORIG_ABOVE
    ce._pick_hang_below = _ORIG_BELOW


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


def run_day(arr, gate, recipe_base, vix, struct_disabled):
    recipe = deepcopy(recipe_base)
    recipe["session_side_gate"] = gate
    recipe["struct_disabled"] = struct_disabled
    O, H, L, C, V, T = arr
    _patch_always_lo()
    try:
        trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    finally:
        _restore()
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


def run_window(label, days, source_map, recipe_base, vix):
    enabled_net, disabled_net = {}, {}
    enabled_n, disabled_n = {}, {}
    for d in days:
        arr = load_arrays(d, source_map)
        if arr is None:
            continue
        gate = continuous_gate_for_day(d, arr[5], source=SOURCE_FOR_DAY.get(d, "tx_1m_fullnight_cache_full.json"))
        net_e, n_e = run_day(arr, gate, recipe_base, vix, struct_disabled=False)
        net_d, n_d = run_day(arr, gate, recipe_base, vix, struct_disabled=True)
        enabled_net[d], disabled_net[d] = net_e, net_d
        enabled_n[d], disabled_n[d] = n_e, n_d

    deltas = [disabled_net[d] - enabled_net[d] for d in enabled_net]
    stats = paired_stats(deltas)
    i_max = max(range(len(deltas)), key=lambda i: abs(deltas[i])) if deltas else None
    excl_mean = None
    if i_max is not None and len(deltas) > 1:
        excl = deltas[:i_max] + deltas[i_max + 1:]
        excl_mean = st.mean(excl) if excl else None

    print(f"=== {label} ({len(days)} days requested, n={stats['n']}) ===")
    print(f"struct_enabled: total_net={sum(enabled_net.values()):.1f} total_trades={sum(enabled_n.values())}")
    print(f"struct_disabled: total_net={sum(disabled_net.values()):.1f} total_trades={sum(disabled_n.values())}")
    print(f"delta(disabled-enabled): sum={sum(deltas):.1f} mean={stats['mean']:.2f} std={stats['std']:.2f} "
          f"t={stats['t']:.3f} p={stats['p']} excl_top_day_mean={excl_mean}")
    return dict(
        n=stats["n"], mean=stats["mean"], t=stats["t"], p=stats["p"], sum_delta=round(sum(deltas), 1),
        excl_top_day_mean=excl_mean,
        enabled_total_net=round(sum(enabled_net.values()), 1), enabled_total_trades=sum(enabled_n.values()),
        disabled_total_net=round(sum(disabled_net.values()), 1), disabled_total_trades=sum(disabled_n.values()),
    )


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")
    recipe_base["session_pv_book"] = specialized_cell_book()

    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]

    results = {}
    results["IS_22d"] = run_window("IN-SAMPLE(22d)", IS_DAYS, SOURCE_FOR_DAY, recipe_base, vix)
    results["OOS_66d"] = run_window(
        "OUT-OF-SAMPLE(66d)", oos_days, {d: "tx_1m_fullnight_cache_full.json" for d in oos_days},
        recipe_base, vix)

    import json
    out_path = "reports/research/channel_lab/tmf_always_lo_struct_disabled_test_result.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
