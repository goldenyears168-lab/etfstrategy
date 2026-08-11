#!/usr/bin/env python3
"""2026-08-10: user wants NQ used to calibrate the INNER-CIRCLE DISTANCE
(not direction -- direction is already 100% handled via cell.bias +
session_side_gate, confirmed in tmf_always_lo_trade_feature_scan.py).

causal_engine.py already has an unused mechanism for exactly this:
nq_hang_adj() (recipe key 'nq_calib') pulls the hang band by NQ overnight
conviction magnitude, clamped back into [hang_lo, hang_hi] -- it has never
been wired to real NQ data in this repo (grep finds zero builders for the
'nq_on_1m' input it needs). Built one here (nq_on_1m_for_day), reusing the
SAME PIT-safe nq_signal.futures_overnight_at()/price_at_or_before() calls
already used tonight for the (already-validated) continuous direction gate
-- forward-fill only from values at-or-before the bar's own timestamp, per
bar, per day. No lookahead: verified causal_engine.py's own nq_on[i]
construction only forward-fills from hm' <= hm within the SAME day.

Screens on IS(22d) only (cheap), narrows to top candidates, then confirms
on OOS(66d). Base recipe = always_lo (current leading candidate) +
struct_disabled=True (found 2026-08-10, OOS p=0.0003) + v1.5.0 cell book.
"""
from __future__ import annotations

import statistics as st
import sys
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

import tmf_channel.causal_engine as ce  # noqa: E402
import tmf_channel.nq_gate as nq_gate_mod  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel import nq_signal  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402

_ORIG_ABOVE = ce._pick_hang_above
_ORIG_BELOW = ce._pick_hang_below
_TZ = ZoneInfo("Asia/Taipei")


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


def nq_on_1m_for_day(day: str, T: list[str]) -> dict[str, float]:
    """{HH:MM: nq_overnight_pct}, causal (only at-or-before data), matches
    the SAME snapshot mechanism as continuous_gate_for_day() but returns
    the raw magnitude instead of the L/S/none side."""
    bundle = nq_gate_mod.get_cached("nq_futures_1h", 1800.0, nq_gate_mod._load_futures_bundle)
    nq_1h, es_1h, nq_d, es_d, us_dates = bundle
    out = {}
    for t in T:
        dt_tw = datetime.fromisoformat(t).astimezone(_TZ)
        hm = dt_tw.strftime("%H:%M")
        snap = nq_signal.futures_overnight_at(
            dt_tw, nq_1h=nq_1h, es_1h=es_1h, nq_d=nq_d, es_d=es_d, us_dates=us_dates
        )
        if snap is not None and snap.get("nq_overnight_pct") is not None:
            out[hm] = float(snap["nq_overnight_pct"])
    return out


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


def run_day(arr, gate, nq_1m, recipe_base, vix, calib_overrides):
    recipe = deepcopy(recipe_base)
    recipe["session_side_gate"] = gate
    recipe["nq_on_1m"] = {arr[5][0].split("T")[0]: nq_1m}
    recipe.update(calib_overrides)
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


def run_window(label, days, source_map, recipe_base, vix, calib_overrides):
    arrays = {}
    gates = {}
    nq1ms = {}
    for d in days:
        arr = load_arrays(d, source_map)
        if arr is None:
            continue
        arrays[d] = arr
        gates[d] = continuous_gate_for_day(d, arr[5], source=SOURCE_FOR_DAY.get(d, "tx_1m_fullnight_cache_full.json"))
        nq1ms[d] = nq_on_1m_for_day(d, arr[5])

    baseline_net = {}
    for d, arr in arrays.items():
        net, _n = run_day(arr, gates[d], nq1ms[d], recipe_base, vix, {"nq_calib": "none"})
        baseline_net[d] = net

    deltas, ns = [], []
    for d, arr in arrays.items():
        net, n = run_day(arr, gates[d], nq1ms[d], recipe_base, vix, calib_overrides)
        deltas.append(net - baseline_net[d])
        ns.append(n)

    stats = paired_stats(deltas)
    print(f"{label:10s} {str(calib_overrides):70s} n={stats['n']:2d} "
          f"mean={stats['mean']:8.2f} t={stats['t']:6.2f} p={stats['p']} "
          f"sum_delta={sum(deltas):9.1f} trades={sum(ns)}")
    return dict(mean=stats["mean"], t=stats["t"], p=stats["p"], sum_delta=round(sum(deltas), 1),
                n=stats["n"], trades=sum(ns))


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")
    recipe_base["session_pv_book"] = specialized_cell_book()
    recipe_base["struct_disabled"] = True

    grid = []
    for mode in ("always_nq", "div_tx"):
        for gamma in (3.0, 6.0, 9.0):
            for cap in (8.0, 15.0):
                grid.append({"nq_calib": mode, "nq_calib_gamma": gamma, "nq_calib_cap": cap})

    print("=== IS(22d) screen: always_lo + struct_disabled + nq_calib variants vs nq_calib=none ===")
    is_results = {}
    for combo in grid:
        r = run_window("IS_22d", IS_DAYS, SOURCE_FOR_DAY, recipe_base, vix, combo)
        is_results[str(combo)] = dict(combo=combo, **r)

    ranked = sorted(is_results.values(), key=lambda r: r["mean"], reverse=True)
    top = [r for r in ranked if r["mean"] > 0][:3]
    print(f"\n=== top {len(top)} candidates by IS mean delta, confirming on OOS(66d) ===")

    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]
    oos_source_map = {d: "tx_1m_fullnight_cache_full.json" for d in oos_days}
    oos_results = {}
    for r in top:
        oos_r = run_window("OOS_66d", oos_days, oos_source_map, recipe_base, vix, r["combo"])
        oos_results[str(r["combo"])] = dict(combo=r["combo"], is_result=r, oos_result=oos_r)

    import json
    out_path = "reports/research/channel_lab/tmf_nq_width_calib_sweep_result.json"
    with open(out_path, "w") as f:
        json.dump(dict(is_screen=is_results, oos_confirm=oos_results), f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
