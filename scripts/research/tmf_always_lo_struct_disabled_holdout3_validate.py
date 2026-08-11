#!/usr/bin/env python3
"""2026-08-10: third-window validation of the current leading candidate
(always_lo hang anchor + struct_disabled=True) before touching live
order-layer code. Same pattern as tmf_night_expand_dn_holdout3_validate.py
(which this candidate must clear too -- that one FAILED here, this is the
gate that actually catches overfit candidates, not a formality).

Fixed candidate, no re-search: always_lo (forced inner-boundary hang
anchor) + struct_disabled=True. Tests against 3 independent 2025 windows
never touched by tonight's IS(22d)/OOS(66d) split:
  julsep25:  2025-07-01..2025-09-30 (65 days)
  octdec25:  2025-10-01..2025-12-31 (62 days)
  janmar26:  2026-01-02..2026-03-31 (55 days)

Does NOT touch src/tmf_channel/causal_engine.py, src/order/*.py.
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
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
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
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def run_day(arr, gate, recipe_base, vix, *, candidate):
    recipe = deepcopy(recipe_base)
    recipe["session_side_gate"] = gate
    if candidate:
        recipe["struct_disabled"] = True
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


def run_window(label, source, recipe_base, vix):
    days = list_days(source=source)
    deltas, base_ns, cand_ns = [], [], []
    for d in days:
        arr = load_arrays(d, source)
        if arr is None:
            continue
        gate = continuous_gate_for_day(d, arr[5], source=source)
        bnet, bn = run_day(arr, gate, recipe_base, vix, candidate=False)
        cnet, cn = run_day(arr, gate, recipe_base, vix, candidate=True)
        deltas.append(cnet - bnet)
        base_ns.append(bn)
        cand_ns.append(cn)

    stats = paired_stats(deltas)
    i_max = max(range(len(deltas)), key=lambda i: abs(deltas[i])) if deltas else None
    excl_mean = None
    if i_max is not None and len(deltas) > 1:
        excl = deltas[:i_max] + deltas[i_max + 1:]
        excl_mean = st.mean(excl) if excl else None

    print(f"=== {label} ({len(days)} days) ===")
    print(f"n_days={stats['n']} baseline_trades={sum(base_ns)} candidate_trades={sum(cand_ns)} "
          f"sum_delta={sum(deltas):.1f} mean={stats['mean']:.2f} std={stats['std']:.2f} "
          f"t={stats['t']:.2f} p={stats['p']} excl_top_day_mean={excl_mean}")
    return dict(n=stats["n"], mean=stats["mean"], t=stats["t"], p=stats["p"],
                sum_delta=round(sum(deltas), 1), excl_top_day_mean=excl_mean,
                baseline_trades=sum(base_ns), candidate_trades=sum(cand_ns))


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")
    recipe_base["session_pv_book"] = specialized_cell_book()

    results = {}
    for label, source in HOLDOUT_SOURCES.items():
        results[label] = run_window(label, source, recipe_base, vix)

    all_n = sum(r["n"] for r in results.values())
    pooled_mean = sum(r["mean"] * r["n"] for r in results.values()) / all_n if all_n else 0.0
    print(f"\n=== POOLED across all 3 holdout windows ({all_n} days) ===")
    print(f"weighted mean delta = {pooled_mean:.2f}/day")
    print("per-window: " + ", ".join(f"{k}={v['mean']:.2f}(p={v['p']})" for k, v in results.items()))

    import json
    out_path = "reports/research/channel_lab/tmf_always_lo_struct_disabled_holdout3_validate_result.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
