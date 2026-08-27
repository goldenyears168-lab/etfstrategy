#!/usr/bin/env python3
"""PV8 classifier VOL_WIN sweep, true re-simulation (2026-08-08 audit).

Assigned dimension: the PV8 classifier mechanism itself (rvol median-window
length), not the threshold values (already settled robust) and not finer
discrete-bucket subdivision (already settled rejected -- both in
config/research.yaml topic tmf-pv16-classifier-refine).

Fork: scripts/research/tx_channel_pv_volwin_engine.py -- byte-identical to
src/tmf_channel/causal_engine.py except rvol_series() lookback is read from
p["pv_vol_win"] instead of hardcoded 20. Candidates 10/15/30/40 vs. the
live baseline (VOL_WIN=20, i.e. pv_vol_win absent / fork with pv_vol_win=20
as an identity sanity check).

Methodology: day-clustered paired delta vs. live baseline net P&L per day,
t-test across days (n=n_days), per-window breakdown (w83/julsep25/octdec25/
janmar26), single best/worst day exclusion check. All P&L comes from
actually calling simulate() per day per (window, config).
"""
from __future__ import annotations

import json
import statistics as stats
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tx_channel_pv_volwin_engine import simulate as fork_simulate  # noqa: E402

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

CANDIDATES: list[tuple[str, int]] = [
    ("volwin_10", 10),
    ("volwin_15", 15),
    ("volwin_20_identity", 20),  # sanity check: must match live baseline exactly
    ("volwin_30", 30),
    ("volwin_40", 40),
]


def _arrays_from_rows(rows: list[dict[str, Any]], day: str):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def run_window(name: str, cache_name: str, vix: dict[str, float]) -> dict[str, Any]:
    days = list_days(cache_name)
    baseline: dict[str, float] = {}
    cand_nets: dict[str, dict[str, float]] = {c[0]: {} for c in CANDIDATES}

    for day in days:
        rows = load_day(day, source=cache_name)
        if not rows:
            continue
        O, H, L, C, V, T = _arrays_from_rows(rows, day)

        recipe = deepcopy(PAPER_RECIPE)
        tr, *_ = live_simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
        baseline[day] = sum(t["pnl"] for t in tr)

        for label, win in CANDIDATES:
            crecipe = deepcopy(PAPER_RECIPE)
            crecipe["pv_vol_win"] = win
            ctr, *_ = fork_simulate(O, H, L, C, V, T, crecipe, vix_delta=vix)
            cand_nets[label][day] = sum(t["pnl"] for t in ctr)

    out: dict[str, Any] = {"window": name, "cache": cache_name, "n_days": len(baseline)}
    out["_baseline_nets"] = baseline

    for label, _win in CANDIDATES:
        cur = cand_nets[label]
        common = sorted(set(baseline) & set(cur))
        deltas = [cur[d] - baseline[d] for d in common]
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
        else:
            worst_i = best_i = None

        out[label] = {
            "_deltas": deltas,
            "_cand_nets": {d: cur[d] for d in common},
            "n_days": n,
            "mean_delta_pt_per_day": round(mean_d, 2),
            "sd_delta": round(sd_d, 2),
            "t_stat": round(t_stat, 3) if t_stat == t_stat else None,
            "p_value": round(pval, 4) if pval == pval else None,
            "sum_delta_pt": round(sum(deltas), 1),
            "worst_day": (common[worst_i], round(deltas[worst_i], 1)) if n else None,
            "best_day": (common[best_i], round(deltas[best_i], 1)) if n else None,
        }

    return out


def main() -> None:
    vix = load_vixtwn_delta() or {}
    results = [run_window(name, cache, vix) for name, cache in WINDOWS]

    pooled: dict[str, Any] = {}
    for label, _win in CANDIDATES:
        all_deltas: list[float] = []
        all_base: list[float] = []
        all_cand: list[float] = []
        for r in results:
            all_deltas.extend(r[label]["_deltas"])
            for d in sorted(r[label]["_cand_nets"]):
                all_base.append(r["_baseline_nets"][d])
                all_cand.append(r[label]["_cand_nets"][d])
        n = len(all_deltas)
        mean_d = stats.fmean(all_deltas) if n else 0.0
        sd_d = stats.stdev(all_deltas) if n > 1 else 0.0
        try:
            from scipy import stats as sstats

            pval = float(sstats.ttest_1samp(all_deltas, 0.0).pvalue) if n > 1 else float("nan")
        except Exception:
            pval = float("nan")

        base_mean = stats.fmean(all_base) if all_base else 0.0
        base_sd = stats.stdev(all_base) if len(all_base) > 1 else 0.0
        cand_mean = stats.fmean(all_cand) if all_cand else 0.0
        cand_sd = stats.stdev(all_cand) if len(all_cand) > 1 else 0.0

        # single best/worst delta-day exclusion, pooled
        if n:
            worst_i = min(range(n), key=lambda i: all_deltas[i])
            best_i = max(range(n), key=lambda i: all_deltas[i])
            mean_wo_worst = stats.fmean([d for i, d in enumerate(all_deltas) if i != worst_i]) if n > 1 else None
            mean_wo_best = stats.fmean([d for i, d in enumerate(all_deltas) if i != best_i]) if n > 1 else None
        else:
            mean_wo_worst = mean_wo_best = None

        pooled[label] = {
            "n_days": n,
            "mean_delta_pt_per_day": round(mean_d, 2),
            "sd_delta": round(sd_d, 2),
            "sum_delta_pt": round(sum(all_deltas), 1),
            "p_value": round(pval, 4) if pval == pval else None,
            "n_windows_positive_sum": sum(1 for r in results if r[label]["sum_delta_pt"] > 0),
            "baseline_mean": round(base_mean, 2),
            "baseline_sd": round(base_sd, 2),
            "baseline_sharpe_like": round(base_mean / base_sd, 4) if base_sd else None,
            "candidate_mean": round(cand_mean, 2),
            "candidate_sd": round(cand_sd, 2),
            "candidate_sharpe_like": round(cand_mean / cand_sd, 4) if cand_sd else None,
            "mean_delta_excl_worst_day": round(mean_wo_worst, 2) if mean_wo_worst is not None else None,
            "mean_delta_excl_best_day": round(mean_wo_best, 2) if mean_wo_best is not None else None,
        }

    for r in results:
        for label, _win in CANDIDATES:
            r[label].pop("_deltas", None)
            r[label].pop("_cand_nets", None)
        r.pop("_baseline_nets", None)

    out = {
        "candidates": [c[0] for c in CANDIDATES],
        "pooled": pooled,
        "per_window": results,
    }
    OUT = (
        Path(__file__).resolve().parent.parent.parent
        / "reports"
        / "research"
        / "channel_lab"
        / "pv_volwin_walkforward_0808.json"
    )
    OUT.write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps(out, indent=2, default=str))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
