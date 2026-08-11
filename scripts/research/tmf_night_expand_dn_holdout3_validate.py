#!/usr/bin/env python3
"""Third-window validation (2026-08-10) of the night|expand_dn unblock
candidate before deploying it live: hang_lo=16.0, hang_hi=30.0,
early_fill_gamma=5.0, max_hold_bars=16, block=[] (chosen by the 16-agent
retune workflow, ADOPT verdict, IS p=0.0069 / OOS p=0.0299 on the 22-day +
66-day windows already used tonight -- see
tmf_continuous_gate_cell_tune_night_expand_dn.py and
tmf_continuous_gate_16cell_final_combine.py).

This does NOT re-search for a candidate -- the parameters are FIXED to the
already-chosen winner. It tests that fixed candidate against 3 genuinely
independent, older holdout windows never touched by tonight's IS/OOS split:
  julsep25:  2025-07-01..2025-09-30 (65 days)
  octdec25:  2025-10-01..2025-12-31 (62 days)
  janmar26:  2026-01-02..2026-03-31 (55 days)
~182 more days, a different market regime (2025 vs the April-August 2026
window used so far), the strongest available independent check before
touching live order-layer code.

Does NOT touch src/tmf_channel/causal_engine.py, src/tmf_channel/nq_gate.py,
src/order/*.py, config/order.yaml, .env, launchd/, scripts/order/,
config/strategy.yaml, config/strategies.yaml.
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
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402

MY_SESS = "night"
MY_PV = "expand_dn"
WINNING_OVERRIDES = dict(
    block=[], hang_lo=16.0, hang_hi=30.0, early_fill_gamma=5.0, max_hold_bars=16,
)

HOLDOUT_SOURCES = {
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}


def build_book(overrides: dict | None):
    book = deepcopy(specialized_cell_book())
    if overrides:
        book[MY_SESS][MY_PV].update(overrides)
    return book


def day_in_window(hm: str) -> bool:
    return "08:45" <= hm < "13:45"


def run_book(O, H, L, C, V, T, gate, book, recipe_base, vix):
    recipe = deepcopy(recipe_base)
    recipe["session_pv_book"] = book
    recipe["session_side_gate"] = gate
    trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    net, n = 0.0, 0
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


def paired_stats(deltas: list[float]):
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


def run_window(label: str, source: str, recipe_base, vix, baseline_book, cand_book):
    days = list_days(source=source)
    deltas, ns = [], []
    for d in days:
        rows = load_day(d, source=source)
        if not rows:
            continue
        O = [float(r["o"]) for r in rows]
        H = [float(r["h"]) for r in rows]
        L = [float(r["l"]) for r in rows]
        C = [float(r["c"]) for r in rows]
        V = [float(r.get("v") or 0) for r in rows]
        T = [f"{d}T{r.get('t')}:00.000+08:00" for r in rows]
        gate = continuous_gate_for_day(d, T, source=source)
        bnet, _ = run_book(O, H, L, C, V, T, gate, baseline_book, recipe_base, vix)
        cnet, cn = run_book(O, H, L, C, V, T, gate, cand_book, recipe_base, vix)
        deltas.append(cnet - bnet)
        ns.append(cn)

    stats = paired_stats(deltas)
    total_trades = sum(ns)
    i_max = max(range(len(deltas)), key=lambda i: abs(deltas[i])) if deltas else None
    excl_mean = None
    if i_max is not None and len(deltas) > 1:
        excl = deltas[:i_max] + deltas[i_max + 1:]
        excl_mean = st.mean(excl) if excl else None

    print(f"=== {label} ({len(days)} days) ===")
    print(f"n_days={stats['n']} total_trades={total_trades} sum_delta={sum(deltas):.1f} "
          f"mean={stats['mean']:.2f} std={stats['std']:.2f} t={stats['t']:.2f} p={stats['p']} "
          f"excl_top_day_mean={excl_mean}")
    return dict(n=stats["n"], total_trades=total_trades, sum_delta=round(sum(deltas), 1),
                mean=stats["mean"], std=stats["std"], t=stats["t"], p=stats["p"],
                excl_top_day_mean=excl_mean)


def main():
    # 2026-08-10: lookback_days=60 silently returned "none" (no signal) for
    # 82% of the original 66-day OOS window and ALL of these 3 much-older
    # holdout windows -- Yahoo actually supports 1h data back to ~2025-05,
    # confirmed by direct fetch; the earlier narrow window was simply not
    # requesting far enough back. Use a wide window here so these
    # 2025-07..2026-03 days get real gate coverage, not spurious blocks.
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")

    baseline_book = build_book(None)
    cand_book = build_book(WINNING_OVERRIDES)

    results = {}
    for label, source in HOLDOUT_SOURCES.items():
        results[label] = run_window(label, source, recipe_base, vix, baseline_book, cand_book)

    all_deltas_n = sum(r["n"] for r in results.values())
    pooled_mean = sum(r["mean"] * r["n"] for r in results.values()) / all_deltas_n
    print(f"\n=== POOLED across all 3 holdout windows ({all_deltas_n} days) ===")
    print(f"weighted mean delta = {pooled_mean:.2f}/day")
    print(f"per-window means: " + ", ".join(f"{k}={v['mean']:.2f}(p={v['p']})" for k, v in results.items()))

    out_path = "reports/research/channel_lab/tmf_night_expand_dn_holdout3_validate_result.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
