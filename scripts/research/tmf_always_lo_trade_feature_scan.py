#!/usr/bin/env python3
"""2026-08-10: under the always_lo hang-anchor variant (see
tmf_hang_lo_vs_hi_anchor_compare.py / tmf_hang_lo_vs_hi_scorecard.py),
collect every individual OOS(66d) trade with its entry-time features and
compare the best vs worst deciles -- looking for an early-observable signal
that could gate/complement always_lo (e.g. NQ alignment, session, regime,
hour), before designing any hybrid or NQ-calibration mechanism.

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


def hhmm_of(iso_t):
    return str(iso_t).split("T", 1)[1][:5] if "T" in str(iso_t) else str(iso_t)[:5]


def session_of(hm):
    return "day" if "08:45" <= hm < "13:45" else "night"


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")
    recipe_base["session_pv_book"] = specialized_cell_book()

    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]

    all_trades = []
    for d in oos_days:
        arr = load_arrays(d, "tx_1m_fullnight_cache_full.json")
        if arr is None:
            continue
        gate = continuous_gate_for_day(d, arr[5], source="tx_1m_fullnight_cache_full.json")
        recipe = deepcopy(recipe_base)
        recipe["session_side_gate"] = gate
        O, H, L, C, V, T = arr
        _patch_always_lo()
        try:
            trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
        finally:
            _restore()
        for t in trades:
            et = str(t.get("et") or "")
            hm = hhmm_of(et)
            sess = session_of(hm)
            hour = hm[:2]
            nq_at_entry = gate.get(et, "none")
            all_trades.append(dict(
                day=d, side=t["s"], pnl=t["pnl"], hold=t.get("hold"),
                regime_e=t.get("regime_e"), why_exit=t.get("why"),
                session=sess, hour=hour, nq_at_entry=nq_at_entry,
                nq_aligned=(nq_at_entry == t["s"]),
            ))

    all_trades.sort(key=lambda x: x["pnl"])
    n = len(all_trades)
    decile = max(1, n // 10)
    worst = all_trades[:decile]
    best = all_trades[-decile:]
    mid = all_trades[decile:-decile]

    print(f"total trades: {n}, decile size: {decile}")
    print(f"worst decile sum={sum(t['pnl'] for t in worst):.1f} mean={st.mean([t['pnl'] for t in worst]):.1f}")
    print(f"best decile sum={sum(t['pnl'] for t in best):.1f} mean={st.mean([t['pnl'] for t in best]):.1f}")
    print(f"middle 80% sum={sum(t['pnl'] for t in mid):.1f} mean={st.mean([t['pnl'] for t in mid]):.1f}")

    def dist(group, key):
        c = {}
        for t in group:
            c[t[key]] = c.get(t[key], 0) + 1
        tot = len(group)
        return {k: round(v / tot * 100, 1) for k, v in sorted(c.items(), key=lambda kv: -kv[1])}

    for key in ("session", "regime_e", "why_exit", "hour", "nq_at_entry", "side"):
        print(f"\n--- {key} distribution ---")
        print("worst decile:", dist(worst, key))
        print("best  decile:", dist(best, key))
        print("middle 80%  :", dist(mid, key))

    print("\n--- nq_aligned (entry side matches NQ gate side) ---")
    for label, group in (("worst", worst), ("best", best), ("middle", mid)):
        aligned = sum(1 for t in group if t["nq_aligned"])
        print(f"{label}: aligned={aligned}/{len(group)} ({aligned/len(group)*100:.1f}%) "
              f"mean_pnl_aligned={st.mean([t['pnl'] for t in group if t['nq_aligned']]) if aligned else None} "
              f"mean_pnl_not={st.mean([t['pnl'] for t in group if not t['nq_aligned']]) if aligned < len(group) else None}")

    print("\n--- hold time (bars) ---")
    for label, group in (("worst", worst), ("best", best), ("middle", mid)):
        holds = [t["hold"] for t in group if t["hold"] is not None]
        print(f"{label}: mean_hold={st.mean(holds):.1f} median={st.median(holds):.1f}" if holds else f"{label}: no hold data")

    import json
    out_path = "reports/research/channel_lab/tmf_always_lo_trade_feature_scan_result.json"
    with open(out_path, "w") as f:
        json.dump(dict(n_trades=n, all_trades=all_trades), f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
