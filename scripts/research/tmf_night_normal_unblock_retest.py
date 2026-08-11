#!/usr/bin/env python3
"""2026-08-11: night|normal is permanently block=["L","S"] in the live
CELL_TUNE_V2 book (config/strategy.yaml: applied 2026-08-06, before the
continuous per-bar NQ gate existed and well before tonight's NQ 1h
forming-bar fix in us_futures_overnight.price_at_or_before). Since the
underlying entry-side filter (session_side_gate / NQ direction) has
materially changed twice since that block decision was made, re-test
whether unblocking night|normal still loses under the CURRENT live
recipe (smart-structure-pick, NOT the rejected always_lo line) + the
NOW-FIXED continuous gate, before assuming the old decision still holds.

Baseline = current live book unchanged. Candidate = same book with
night|normal's block overridden to [] (everything else identical,
including hang_lo/hi/early_fill_gamma/max_hold_bars for that cell).
"""
from __future__ import annotations

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
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})
IS_DAYS = JULY_DAYS + AUG_DAYS

HOLDOUT_SOURCES = {
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}


def build_unblocked_book():
    book = deepcopy(specialized_cell_book())
    book["night"]["normal"]["block"] = []
    return book


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


def run_book(arr, gate, book, recipe_base, vix):
    recipe = deepcopy(recipe_base)
    recipe["session_side_gate"] = gate
    recipe["session_pv_book"] = book
    O, H, L, C, V, T = arr
    trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    net_all = round(sum(t["pnl"] for t in trades), 1)
    # isolate night|normal's own contribution for visibility
    net_cell = 0.0
    n_cell = 0
    for t in trades:
        if t.get("regime_e") != "normal":
            continue
        et = str(t.get("et") or "")
        hm = et.split("T", 1)[1][:5] if "T" in et else et[:5]
        sess = "day" if "08:45" <= hm < "13:45" else "night"
        if sess != "night":
            continue
        net_cell += float(t["pnl"])
        n_cell += 1
    return net_all, len(trades), round(net_cell, 1), n_cell


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


def run_window(label, days, source_map, recipe_base, vix, baseline_book, cand_book):
    deltas, cell_nets, cell_ns, total_trades = [], [], [], []
    for d in days:
        source = source_map.get(d, "tx_1m_fullnight_cache_full.json") if isinstance(source_map, dict) else source_map
        arr = load_arrays(d, source)
        if arr is None:
            continue
        gate = continuous_gate_for_day(d, arr[5], source=SOURCE_FOR_DAY.get(d, "tx_1m_fullnight_cache_full.json"))
        bnet, bn, _bcell, _bn_cell = run_book(arr, gate, baseline_book, recipe_base, vix)
        cnet, cn, ccell, cn_cell = run_book(arr, gate, cand_book, recipe_base, vix)
        deltas.append(cnet - bnet)
        cell_nets.append(ccell)
        cell_ns.append(cn_cell)
        total_trades.append(cn)

    stats = paired_stats(deltas)
    i_max = max(range(len(deltas)), key=lambda i: abs(deltas[i])) if deltas else None
    excl_mean = None
    if i_max is not None and len(deltas) > 1:
        excl = deltas[:i_max] + deltas[i_max + 1:]
        excl_mean = st.mean(excl) if excl else None

    print(f"=== {label} ({len(deltas)} days) ===")
    print(f"overall delta(unblocked-baseline): sum={sum(deltas):.1f} mean={stats['mean']:.2f} "
          f"t={stats['t']:.3f} p={stats['p']} excl_top_day_mean={excl_mean}")
    print(f"night|normal cell alone: total_trades={sum(cell_ns)} net={sum(cell_nets):.1f}")
    return dict(n=stats["n"], mean=stats["mean"], t=stats["t"], p=stats["p"],
                sum_delta=round(sum(deltas), 1), excl_top_day_mean=excl_mean,
                cell_trades=sum(cell_ns), cell_net=round(sum(cell_nets), 1))


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")

    baseline_book = specialized_cell_book()
    cand_book = build_unblocked_book()

    results = {}
    results["IS_22d"] = run_window("IS_22d", IS_DAYS, SOURCE_FOR_DAY, recipe_base, vix, baseline_book, cand_book)
    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]
    results["OOS_66d"] = run_window("OOS_66d", oos_days, "tx_1m_fullnight_cache_full.json", recipe_base, vix, baseline_book, cand_book)

    import json
    out_path = "reports/research/channel_lab/tmf_night_normal_unblock_retest_result.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
