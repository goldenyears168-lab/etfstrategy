#!/usr/bin/env python3
"""Sticky-gate walk-forward (Revalidate phase, 2026-08-08).

Question: does gating brand-new entries (how_final=="enter" only) on the
causal, oracle-free "sticky coarse channel key" (see
tx_channel_sticky_gate_engine.sticky_allow_causal) beat the current live
PAPER_RECIPE baseline on net P&L while reducing order churn (thrash), across
all 4 sanctioned windows -- not just the 3-session single-day-artifact sweep
in slow_cell_sticky_beat_pv_report_0808.md?

Engine: scripts/research/tx_channel_sticky_gate_engine.py (TRUE fork of
src/tmf_channel/causal_engine.py, byte-identical when sticky_gate_mode=None
-- verified against the frozen live engine on 5 sample days before this
driver was trusted). All P&L here comes from actually calling this forked
simulate() per day per (window, config) -- never post-hoc filtering.

Methodology (per task's stated rules):
  - Day-clustered significance: one net-P&L number per calendar-day, paired
    delta vs the live baseline, t-test across days (n=n_days), never pooled
    trades/bars.
  - Report per-window (w83/julsep25/octdec25/janmar26), not just pooled.
  - Check whether excluding the single best/worst delta day flips sign.
  - Churn/thrash: the engine's own place/cancel rail events (kind in
    {"place","cancel"}), per side (S/L), walked in time order -- a thrash
    pair is a place immediately followed by a cancel of the SAME side (or
    vice versa) within <=2 bars. This is the real engine's literal order
    churn, not a proxy -- and matches the task's "short round-trips with
    dwell<=2 bars" definition exactly.
"""
from __future__ import annotations

import json
import statistics as stats
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tx_channel_sticky_gate_engine import simulate as fork_simulate  # noqa: E402

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.causal_engine import simulate as live_simulate  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta  # noqa: E402

WINDOWS: list[tuple[str, str]] = [
    ("w83", "tx_1m_fullnight_cache_full.json"),
    ("julsep25", "tx_1m_julsep_holdout_cache.json"),
    ("octdec25", "tx_1m_octdec_holdout_cache.json"),
    ("janmar26", "tx_1m_janmar_holdout_cache.json"),
]

# CANDIDATES: (label, sticky_gate_mode, hyst_bars).
# Report's own honest same-protocol winner: sticky_no_chop, hyst=10 (net
# +59.8 over baseline in the toy 1m proxy) -- start there. Also try hyst=0
# (no hysteresis) and sticky_and_pv (task's fallback if with_trend hang
# framing doesn't map -- it doesn't, so we test the allow-mode substitute).
CANDIDATES: list[tuple[str, str, int]] = [
    ("sticky_no_chop_h10", "sticky_no_chop", 10),
    ("sticky_no_chop_h0", "sticky_no_chop", 0),
    ("sticky_and_pv_h10", "sticky_and_pv", 10),
]


def _arrays_from_rows(day: str, rows: list[dict[str, Any]]):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    # rows' "t" is a bare "HH:MM" (see tmf_channel.cache_store.load_day) --
    # must carry the calendar-date prefix so causal_engine._day(ts) resolves
    # to the real session date, not garbage (see vix_session_bias bug note).
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def thrash_pairs(events: list[dict[str, Any]]) -> int:
    """Place/cancel round-trips <=2 bars apart, per side, summed."""
    total = 0
    for side in ("S", "L"):
        seq = sorted(
            (e for e in events if e.get("side") == side and e.get("kind") in ("place", "cancel")),
            key=lambda e: e["t"],
        )
        last_i, last_kind = -999, ""
        for e in seq:
            i, kind = e["t"], e["kind"]
            if kind == "place" and last_kind == "cancel" and i - last_i <= 2:
                total += 1
            elif kind == "cancel" and last_kind == "place" and i - last_i <= 2:
                total += 1
            last_i, last_kind = i, kind
    return total


def run_window(name: str, cache_name: str, vix: dict[str, float]) -> dict[str, Any]:
    days = list_days(cache_name)
    baseline: dict[str, dict[str, Any]] = {}
    cand_results: dict[str, dict[str, dict[str, Any]]] = {c[0]: {} for c in CANDIDATES}

    for day in days:
        rows = load_day(day, source=cache_name)
        if not rows:
            continue
        O, H, L, C, V, T = _arrays_from_rows(day, rows)

        recipe = deepcopy(PAPER_RECIPE)
        tr, ev, ws, wl, rvol, regime, op = live_simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
        baseline[day] = dict(
            net=sum(t["pnl"] for t in tr), n_trades=len(tr), thrash=thrash_pairs(ev)
        )

        for label, mode, hyst in CANDIDATES:
            crecipe = deepcopy(PAPER_RECIPE)
            crecipe["sticky_gate_mode"] = mode
            crecipe["sticky_hyst_bars"] = hyst
            ctr, cev, *_ = fork_simulate(O, H, L, C, V, T, crecipe, vix_delta=vix)
            n_stk_rej = sum(1 for e in cev if e.get("note") == "sticky_gate")
            cand_results[label][day] = dict(
                net=sum(t["pnl"] for t in ctr),
                n_trades=len(ctr),
                thrash=thrash_pairs(cev),
                sticky_rejects=n_stk_rej,
            )

    out: dict[str, Any] = {"window": name, "cache": cache_name, "n_days": len(baseline)}

    for label, _mode, _hyst in CANDIDATES:
        cur = cand_results[label]
        common = sorted(set(baseline) & set(cur))
        deltas = [cur[d]["net"] - baseline[d]["net"] for d in common]
        base_nets = [baseline[d]["net"] for d in common]
        cand_nets = [cur[d]["net"] for d in common]
        base_thrash = sum(baseline[d]["thrash"] for d in common)
        cand_thrash = sum(cur[d]["thrash"] for d in common)
        base_trades = sum(baseline[d]["n_trades"] for d in common)
        cand_trades = sum(cur[d]["n_trades"] for d in common)

        n = len(deltas)
        mean_d = stats.fmean(deltas) if n else 0.0
        sd_d = stats.stdev(deltas) if n > 1 else 0.0
        se_d = sd_d / (n**0.5) if n > 1 else 0.0
        t_stat = mean_d / se_d if se_d > 0 else float("nan")
        try:
            from scipy import stats as sstats

            pval = float(sstats.ttest_1samp(deltas, 0.0).pvalue) if n > 1 else float("nan")
        except Exception:
            pval = float("nan")

        if n:
            worst_i = min(range(n), key=lambda i: deltas[i])
            best_i = max(range(n), key=lambda i: deltas[i])
            mean_wo_worst = stats.fmean([d for i, d in enumerate(deltas) if i != worst_i]) if n > 1 else None
            mean_wo_best = stats.fmean([d for i, d in enumerate(deltas) if i != best_i]) if n > 1 else None
        else:
            worst_i = best_i = None
            mean_wo_worst = mean_wo_best = None

        out[label] = {
            "_deltas": deltas,
            "n_days": n,
            "mean_delta_pt_per_day": round(mean_d, 2),
            "sd_delta": round(sd_d, 2),
            "t_stat": round(t_stat, 3) if t_stat == t_stat else None,
            "p_value": round(pval, 4) if pval == pval else None,
            "sum_delta_pt": round(sum(deltas), 1),
            "baseline_sum_net": round(sum(base_nets), 1),
            "candidate_sum_net": round(sum(cand_nets), 1),
            "mean_delta_excl_worst_day": round(mean_wo_worst, 2) if mean_wo_worst is not None else None,
            "mean_delta_excl_best_day": round(mean_wo_best, 2) if mean_wo_best is not None else None,
            "worst_day_delta": round(deltas[worst_i], 1) if n else None,
            "best_day_delta": round(deltas[best_i], 1) if n else None,
            "baseline_thrash_total": base_thrash,
            "candidate_thrash_total": cand_thrash,
            "thrash_reduction_frac": round(1.0 - cand_thrash / base_thrash, 3) if base_thrash else None,
            "baseline_n_trades": base_trades,
            "candidate_n_trades": cand_trades,
            "trades_reduction_frac": round(1.0 - cand_trades / base_trades, 3) if base_trades else None,
            "candidate_sticky_rejects_total": sum(cur[d]["sticky_rejects"] for d in common),
        }

    return out


def main() -> None:
    vix = load_vixtwn_delta() or {}
    results = [run_window(name, cache, vix) for name, cache in WINDOWS]

    pooled: dict[str, Any] = {}
    for label, _mode, _hyst in CANDIDATES:
        all_deltas: list[float] = []
        for r in results:
            all_deltas.extend(r[label]["_deltas"])
        n = len(all_deltas)
        mean_d = stats.fmean(all_deltas) if n else 0.0
        try:
            from scipy import stats as sstats

            pval = float(sstats.ttest_1samp(all_deltas, 0.0).pvalue) if n > 1 else float("nan")
        except Exception:
            pval = float("nan")
        pooled[label] = {
            "n_days": n,
            "mean_delta_pt_per_day": round(mean_d, 2),
            "sum_delta_pt": round(sum(all_deltas), 1),
            "p_value": round(pval, 4) if pval == pval else None,
            "n_windows_positive_sum": sum(
                1 for r in results if r[label]["sum_delta_pt"] > 0
            ),
        }

    for r in results:
        for label, _mode, _hyst in CANDIDATES:
            r[label].pop("_deltas", None)

    out = {
        "candidates": [c[0] for c in CANDIDATES],
        "pooled": pooled,
        "per_window": results,
    }
    OUT = Path(__file__).resolve().parent.parent.parent / "reports" / "research" / "channel_lab" / "sticky_gate_walkforward_0808.json"
    OUT.write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps(out, indent=2, default=str))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
