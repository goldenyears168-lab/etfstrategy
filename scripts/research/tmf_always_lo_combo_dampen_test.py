#!/usr/bin/env python3
"""2026-08-10: user wants always_lo (fixed inner-circle anchor) kept as the
BASE rule (simpler, more visually verifiable) with a "smart assist" layered
on top to fix its julsep25 weakness, rather than switching back to the
smart-structure pick entirely.

Diagnosed root cause of julsep25's worst day (2025-07-17, candidate -338pt
vs baseline): 3 consecutive SAME-DIRECTION (short) losing entries at
climbing prices (22942 -> 22980 -> 23052) while regime_e stayed "normal"
each time -- a sustained SESSION-level uptrend that PV8's per-bar
volume/impulse classification doesn't register as "expand_up"/"climax_up",
so the EXISTING trend_hang_dampen="regime" mode (already live, blocks
counter-trend entries only when the CURRENT bar's regime is expand/climax)
never fires. causal_engine.py already has a "combo" mode that adds a
multi-bar MOMENTUM check (dampen_mom_look=15 bars, dampen_mom_pts=40) on
top of regime -- tests whether that's the missing "smart assist".

Base: always_lo + struct_disabled=True (2026-08-10's leading candidate).
Compares trend_hang_dampen="regime" (current) vs "combo" (candidate assist)
across the SAME 3 holdout windows that already failed the plain candidate,
plus the original 22d IS / 66d OOS for continuity.
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

HOLDOUT_SOURCES = {
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}


def load_arrays(day, source):
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


def run_day(arr, gate, recipe_base, vix, *, dampen_mode):
    recipe = deepcopy(recipe_base)
    recipe["session_side_gate"] = gate
    recipe["struct_disabled"] = True
    recipe["trend_hang_dampen"] = dampen_mode
    _patch_always_lo()
    try:
        O, H, L, C, V, T = arr
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
    regime_net, combo_net = {}, {}
    regime_n, combo_n = {}, {}
    for d in days:
        arr = load_arrays(d, source_map.get(d, "tx_1m_fullnight_cache_full.json") if isinstance(source_map, dict) else source_map)
        if arr is None:
            continue
        gate = continuous_gate_for_day(d, arr[5], source=SOURCE_FOR_DAY.get(d, "tx_1m_fullnight_cache_full.json"))
        rnet, rn = run_day(arr, gate, recipe_base, vix, dampen_mode="regime")
        cnet, cn = run_day(arr, gate, recipe_base, vix, dampen_mode="combo")
        regime_net[d], combo_net[d] = rnet, cnet
        regime_n[d], combo_n[d] = rn, cn

    deltas = [combo_net[d] - regime_net[d] for d in regime_net]
    stats = paired_stats(deltas)
    i_max = max(range(len(deltas)), key=lambda i: abs(deltas[i])) if deltas else None
    excl_mean = None
    if i_max is not None and len(deltas) > 1:
        excl = deltas[:i_max] + deltas[i_max + 1:]
        excl_mean = st.mean(excl) if excl else None

    print(f"=== {label} ({len(regime_net)} days) ===")
    print(f"regime_mode: total_net={sum(regime_net.values()):.1f} trades={sum(regime_n.values())}")
    print(f"combo_mode:  total_net={sum(combo_net.values()):.1f} trades={sum(combo_n.values())}")
    print(f"delta(combo-regime): sum={sum(deltas):.1f} mean={stats['mean']:.2f} t={stats['t']:.2f} "
          f"p={stats['p']} excl_top_day_mean={excl_mean}")
    return dict(regime_total=round(sum(regime_net.values()), 1), combo_total=round(sum(combo_net.values()), 1),
                mean=stats["mean"], t=stats["t"], p=stats["p"], sum_delta=round(sum(deltas), 1))


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")
    recipe_base["session_pv_book"] = specialized_cell_book()

    results = {}
    results["IS_22d"] = run_window("IS_22d", IS_DAYS, SOURCE_FOR_DAY, recipe_base, vix)
    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]
    results["OOS_66d"] = run_window("OOS_66d", oos_days, "tx_1m_fullnight_cache_full.json", recipe_base, vix)
    for label, source in HOLDOUT_SOURCES.items():
        days = list_days(source=source)
        results[label] = run_window(label, days, source, recipe_base, vix)

    import json
    out_path = "reports/research/channel_lab/tmf_always_lo_combo_dampen_test_result.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
