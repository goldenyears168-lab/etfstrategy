#!/usr/bin/env python3
"""Regime-commitment hysteresis walk-forward (TestFix phase, 2026-08-08).

Question: does requiring N consecutive identical raw PV8 classify_pv() bars
before a new label becomes "committed" (used for session_pv_book cell lookup
/ block / hang / quiet gating) reduce flicker/churn without hurting P&L and
without an unacceptable safety cost (bars where a side is raw-blocked but
committed-unblocked, i.e. a resting order would sit unprotected)?

Engine: scripts/research/tx_channel_regime_commitment_engine.py (TRUE fork
of src/tmf_channel/causal_engine.py, byte-identical when regime_commit_n=1
-- verified against the frozen live engine.simulate() on 5 sample days
before this driver was trusted). All P&L reported here comes from actually
calling this forked simulate() per day per (window, N) combination -- never
post-hoc filtering of a single run's trades.

Methodology (per task's stated rules):
  - Day-clustered significance: one net-P&L number per calendar-day
    container, paired delta vs N=1 baseline, t-test across days (n=n_days),
    never pooled trades/bars.
  - Report per-window (w83/julsep25/octdec25/janmar26), not just pooled.
  - Check whether excluding the single best/worst delta day flips sign.
  - Flicker/churn counting reuses the exact short-round-trip-pair method
    from tx_channel_pv8_dwell_flicker_characterize.py (dwell(X)<=2 bars),
    applied to the committed_regime sequence instead of raw regime.
  - Safety-cost accounting is purely descriptive over full days (this is
    characterizing historical classifier/gate behavior, not a live causal
    decision rule -- stated explicitly per the task's methodology rule #1).
"""
from __future__ import annotations

import json
import statistics as stats
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tx_channel_regime_commitment_engine import simulate as fork_simulate  # noqa: E402

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import (  # noqa: E402
    PV8,
    RECIPE_VERSION,
    cell_recipe,
    specialized_cell_book,
)
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta  # noqa: E402

WINDOWS: list[tuple[str, str]] = [
    ("w83", "tx_1m_fullnight_cache_full.json"),
    ("julsep25", "tx_1m_julsep_holdout_cache.json"),
    ("octdec25", "tx_1m_octdec_holdout_cache.json"),
    ("janmar26", "tx_1m_janmar_holdout_cache.json"),
]

CANDIDATES = (2, 3)

BOOK = specialized_cell_book()


def blocked_sides(sess: str, pv: str) -> set[str]:
    c = cell_recipe(BOOK, session=sess, pv=pv)
    return set(c.get("block") or [])


def _arrays_from_rows(day: str, rows: list[dict[str, Any]]):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def _segments_for_day(rows: list[dict[str, Any]], seq: list[str]) -> list[tuple[str, list[str]]]:
    segs: list[tuple[str, list[str]]] = []
    cur_sess: str | None = None
    cur: list[str] = []
    for r, pv in zip(rows, seq):
        sess = r.get("sess")
        if sess not in ("day", "night"):
            if cur:
                segs.append((cur_sess, cur))
            cur_sess, cur = None, []
            continue
        if sess != cur_sess:
            if cur:
                segs.append((cur_sess, cur))
            cur_sess, cur = sess, []
        cur.append(pv)
    if cur:
        segs.append((cur_sess, cur))
    return segs


def _pv_runs(pv_list: list[str]) -> list[tuple[str, int]]:
    runs: list[tuple[str, int]] = []
    i, n = 0, len(pv_list)
    while i < n:
        if pv_list[i] == "na":
            i += 1
            continue
        j = i + 1
        while j < n and pv_list[j] == pv_list[i]:
            j += 1
        runs.append((pv_list[i], j - i))
        i = j
    return runs


def flicker_pairs_for_seq(rows: list[dict[str, Any]], seq: list[str]) -> dict[str, Counter]:
    counts: dict[str, Counter] = {"day": Counter(), "night": Counter()}
    for sess, pv_list in _segments_for_day(rows, seq):
        if sess not in ("day", "night"):
            continue
        runs = _pv_runs(pv_list)
        for i in range(1, len(runs) - 1):
            prev_label, _pl = runs[i - 1]
            cur_label, cur_len = runs[i]
            next_label, _nl = runs[i + 1]
            if prev_label == next_label and prev_label != cur_label and cur_len <= 2:
                key = "<->".join(sorted((prev_label, cur_label)))
                counts[sess][key] += 1
    return counts


def safety_episodes(
    rows: list[dict[str, Any]], regime: list[str], cregime: list[str]
) -> list[tuple[str, int]]:
    """Bars where committed says a side is NOT blocked but raw says it IS.

    Grouped into contiguous episodes (state = 'S' / 'L' / 'both'); PIT
    session boundaries and 'na' bars break an episode (never straddled).
    """
    episodes: list[tuple[str, int]] = []
    cur_state: str | None = None
    cur_len = 0
    for r, raw, comm in zip(rows, regime, cregime):
        sess = r.get("sess")
        state: str | None = None
        if sess in ("day", "night") and raw != "na" and comm != "na":
            viol = blocked_sides(sess, raw) - blocked_sides(sess, comm)
            if viol:
                state = "both" if viol == {"L", "S"} else next(iter(viol))
        if state != cur_state:
            if cur_state is not None:
                episodes.append((cur_state, cur_len))
            cur_state = state
            cur_len = 1 if state is not None else 0
        elif state is not None:
            cur_len += 1
    if cur_state is not None:
        episodes.append((cur_state, cur_len))
    return episodes


def run_window(name: str, cache_name: str, vix: dict[str, float]) -> dict[str, Any]:
    days = list_days(cache_name)
    per_day: dict[int, dict[str, Any]] = {}  # commit_n -> day -> result
    for commit_n in (1, *CANDIDATES):
        per_day[commit_n] = {}
        for day in days:
            rows = load_day(day, source=cache_name)
            if not rows:
                continue
            O, H, L, C, V, T = _arrays_from_rows(day, rows)
            recipe = deepcopy(PAPER_RECIPE)
            recipe["regime_commit_n"] = commit_n
            trades, ev, ws, wl, rvol, regime, op, cregime = fork_simulate(
                O, H, L, C, V, T, recipe, vix_delta=vix
            )
            net = sum(t["pnl"] for t in trades)
            per_day[commit_n][day] = {
                "net": net,
                "n_trades": len(trades),
                "rows": rows,
                "regime": regime,
                "cregime": cregime,
            }

    baseline = per_day[1]
    out: dict[str, Any] = {"window": name, "cache": cache_name, "n_days": len(baseline)}

    for commit_n in CANDIDATES:
        cur = per_day[commit_n]
        common_days = sorted(set(baseline) & set(cur))
        deltas = [cur[d]["net"] - baseline[d]["net"] for d in common_days]
        base_nets = [baseline[d]["net"] for d in common_days]
        cand_nets = [cur[d]["net"] for d in common_days]

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

        # single best/worst day exclusion check
        if n:
            worst_i = min(range(n), key=lambda i: deltas[i])
            best_i = max(range(n), key=lambda i: deltas[i])
            mean_wo_worst = stats.fmean([d for i, d in enumerate(deltas) if i != worst_i]) if n > 1 else None
            mean_wo_best = stats.fmean([d for i, d in enumerate(deltas) if i != best_i]) if n > 1 else None
        else:
            mean_wo_worst = mean_wo_best = None

        # flicker/churn: raw vs committed, day/night, top pairs, pooled counts
        raw_flicker = {"day": Counter(), "night": Counter()}
        committed_flicker = {"day": Counter(), "night": Counter()}
        safety_all: list[tuple[str, int]] = []
        for d in common_days:
            rows = cur[d]["rows"]
            raw_seq = cur[d]["regime"]
            comm_seq = cur[d]["cregime"]
            rf = flicker_pairs_for_seq(rows, raw_seq)
            cf = flicker_pairs_for_seq(rows, comm_seq)
            for sess in ("day", "night"):
                raw_flicker[sess] += rf[sess]
                committed_flicker[sess] += cf[sess]
            safety_all.extend(safety_episodes(rows, raw_seq, comm_seq))

        raw_flicker_total = sum(sum(c.values()) for c in raw_flicker.values())
        committed_flicker_total = sum(sum(c.values()) for c in committed_flicker.values())

        safety_lens = [ln for _s, ln in safety_all]
        safety_by_state = Counter(s for s, _ln in safety_all)

        out[f"N{commit_n}"] = {
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
            "flicker_total_raw": raw_flicker_total,
            "flicker_total_committed": committed_flicker_total,
            "flicker_reduction_frac": round(
                1.0 - committed_flicker_total / raw_flicker_total, 3
            )
            if raw_flicker_total
            else None,
            "flicker_top_pairs_raw": {
                sess: raw_flicker[sess].most_common(3) for sess in ("day", "night")
            },
            "flicker_top_pairs_committed": {
                sess: committed_flicker[sess].most_common(3) for sess in ("day", "night")
            },
            "safety_n_episodes": len(safety_all),
            "safety_n_bars_total": sum(safety_lens),
            "safety_by_state": dict(safety_by_state),
            "safety_median_episode_len": round(stats.median(safety_lens), 1) if safety_lens else 0,
            "safety_max_episode_len": max(safety_lens) if safety_lens else 0,
            "safety_p90_episode_len": (
                round(sorted(safety_lens)[int(0.9 * (len(safety_lens) - 1))], 1)
                if safety_lens
                else 0
            ),
        }

    return out


def main() -> None:
    vix = load_vixtwn_delta() or {}
    results = [run_window(name, cache, vix) for name, cache in WINDOWS]

    # Pooled-across-windows t-test per N: still one number per calendar-day
    # container (day-clustered), just concatenated across all 4 windows'
    # per-day delta lists (n = total days across windows, not trades/bars).
    pooled: dict[str, Any] = {}
    for commit_n in CANDIDATES:
        all_deltas: list[float] = []
        for r in results:
            all_deltas.extend(r[f"N{commit_n}"]["_deltas"])
        n = len(all_deltas)
        mean_d = stats.fmean(all_deltas) if n else 0.0
        try:
            from scipy import stats as sstats

            pval = float(sstats.ttest_1samp(all_deltas, 0.0).pvalue) if n > 1 else float("nan")
        except Exception:
            pval = float("nan")
        pooled[f"N{commit_n}"] = {
            "n_days": n,
            "mean_delta_pt_per_day": round(mean_d, 2),
            "sum_delta_pt": round(sum(all_deltas), 1),
            "p_value": round(pval, 4) if pval == pval else None,
        }

    # strip internal per-day delta lists before printing
    for r in results:
        for commit_n in CANDIDATES:
            r[f"N{commit_n}"].pop("_deltas", None)

    out = {
        "recipe_version": RECIPE_VERSION,
        "candidates": list(CANDIDATES),
        "pooled": pooled,
        "per_window": results,
    }
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
