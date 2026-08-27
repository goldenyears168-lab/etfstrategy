#!/usr/bin/env python3
"""Vol-adaptive stop_pts audit — TRUE re-simulation on a forked causal_engine.

Assigned dimension: 固定停損 stop_pts=150 vs 動態/波動調整停損. Archive audit
found this is a genuinely-unexplored gap (no file scales stop_pts itself by
amplitude/volatility on the actual tmf_channel causal engine; the one file
that combines amp-scaling with stop_pts present scales struct_atr_buffer_mult
instead, leaving stop_pts hardcoded).

Design (ONE concrete alternative, not a sweep):
  - Fork: scripts/research/tmf_channel_causal_engine_volstop.py (byte-
    identical to src/tmf_channel/causal_engine.py except for a
    `stop_vol_scale_mode` opt-in that scales stop_pts at ENTRY time).
  - Mechanism: at entry bar t, compute the causal percentile rank of AMP30[t]
    (linear-recency-weighted 30-bar amplitude, amp30_causal(), STRICTLY bars
    [t-30, t-1]) vs its own trailing 300-bar history (60-bar warmup). If that
    percentile > 0.66 (top tercile = choppiest recent regime), stop_pts is
    multiplied by 0.6 (150 -> 90) for THAT trade only, held fixed for its
    duration; otherwise stop_pts stays 150 (baseline, untouched).
  - Motivation: tonight's own finding that struct_break tail losses
    concentrate specifically in high-volatility bars, and that widening/
    narrowing stop_pts as a flat global constant was already shown to be
    noise-level (20260805_tmf_farcover_retune_adoption.md). A regime-
    conditional (not global) tightening targets exactly the tail-loss subset
    without disturbing the ~2/3 of trades entered in calmer conditions.
  - AMP30/percentile machinery ported verbatim from
    struct_atr_buffer_amp_scaled_lab.py (there wired to struct_atr_buffer_mult;
    here wired to stop_pts for the first time).

Methodology: TRUE re-simulation (forked engine's own simulate()), day-
clustered (n=n_days) paired t-test, per-window breakdown across all 4
sanctioned windows (w83/julsep25/octdec25/janmar26, 265 days total),
single-best/worst-day-exclusion check. PAPER_RECIPE imported live and never
mutated on disk; comparison recipes are in-memory deepcopies only.
"""
from __future__ import annotations

import copy
import json
import statistics
import sys
from math import erf, sqrt
from pathlib import Path

DIR = Path(__file__).resolve().parent
ROOT = DIR.parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(DIR))
sys.path.insert(0, str(SRC))

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.harness import recipe_hash  # noqa: E402
import tmf_channel_causal_engine_volstop as ve  # noqa: E402 -- forked engine

OUT = ROOT / "reports/research/channel_lab/tmf_stop_vol_scale_recheck.json"

WINDOWS = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}


def _arrays(day: str, cache: str):
    rows = load_day(day, source=cache)
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def run_day(day: str, cache: str, recipe: dict, vix: dict) -> float:
    O, H, L, C, V, T = _arrays(day, cache)
    if not C:
        return None
    trades, *_ = ve.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    s = ve.summarize(trades) if trades else {"net": 0.0}
    return round(float(s.get("net") or 0.0), 1)


def paired_ttest(diffs: list[float]):
    n = len(diffs)
    if n < 2:
        return None, None
    m = statistics.mean(diffs)
    sd = statistics.stdev(diffs)
    if sd == 0:
        return (float("inf") if m != 0 else 0.0), (0.0 if m != 0 else 1.0)
    t = m / (sd / (n ** 0.5))
    p = 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2))))
    return t, p


def one_sample_ttest(vals: list[float]):
    n = len(vals)
    m = statistics.mean(vals)
    sd = statistics.stdev(vals) if n > 1 else 0.0
    if sd == 0:
        return m, sd, (float("inf") if m != 0 else 0.0), (0.0 if m != 0 else 1.0)
    t = m / (sd / (n ** 0.5))
    p = 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2))))
    return m, sd, t, p


def main():
    vix = ve.load_vixtwn_delta() or {}
    base_recipe = copy.deepcopy(PAPER_RECIPE)
    alt_recipe = copy.deepcopy(PAPER_RECIPE)
    alt_recipe["stop_vol_scale_mode"] = True
    alt_recipe["stop_vol_scale_hi_thresh"] = 0.66
    alt_recipe["stop_vol_scale_hi_mult"] = 0.6

    print("base recipe_hash =", recipe_hash(base_recipe), flush=True)
    print("alt  recipe_hash =", recipe_hash(alt_recipe), flush=True)

    out = {
        "schema": "tmf-stop-vol-scale-recheck-v1",
        "design": "hi-tercile(amp30 pctile>0.66 @entry) -> stop_pts*=0.6 (150->90); else unchanged",
        "windows": {},
    }
    base_all: list[float] = []
    alt_all: list[float] = []
    day_rows_all: list[tuple[str, str, float, float]] = []  # window, day, base, alt

    for win, cache in WINDOWS.items():
        days = list_days(source=cache)
        base_nets, alt_nets = [], []
        for d in days:
            b = run_day(d, cache, base_recipe, vix)
            a = run_day(d, cache, alt_recipe, vix)
            if b is None or a is None:
                continue
            base_nets.append(b)
            alt_nets.append(a)
            base_all.append(b)
            alt_all.append(a)
            day_rows_all.append((win, d, b, a))
        diffs = [a - b for a, b in zip(alt_nets, base_nets)]
        t, p = paired_ttest(diffs)
        bm, bs, *_ = one_sample_ttest(base_nets)
        am, as_, *_ = one_sample_ttest(alt_nets)
        row = {
            "n_days": len(days),
            "base_mean": round(bm, 1), "base_std": round(bs, 1),
            "alt_mean": round(am, 1), "alt_std": round(as_, 1),
            "mean_diff_alt_minus_base": round(statistics.mean(diffs), 2) if diffs else None,
            "t_stat": round(t, 3) if t is not None else None,
            "p_value": round(p, 4) if p is not None else None,
        }
        out["windows"][win] = row
        print(f"{win:10s} n={row['n_days']:3d} base_mean={row['base_mean']:8.1f} "
              f"alt_mean={row['alt_mean']:8.1f} diff/day={row['mean_diff_alt_minus_base']} "
              f"t={row['t_stat']} p={row['p_value']}", flush=True)

    # pooled
    bm, bs, bt, bp = one_sample_ttest(base_all)
    am, as_, at, ap = one_sample_ttest(alt_all)
    diffs_all = [a - b for a, b in zip(alt_all, base_all)]
    dt, dp = paired_ttest(diffs_all)
    out["pooled"] = {
        "n_days": len(base_all),
        "base_mean": round(bm, 1), "base_std": round(bs, 1), "base_sharpe": round(bm / bs, 4),
        "alt_mean": round(am, 1), "alt_std": round(as_, 1), "alt_sharpe": round(am / as_, 4),
        "paired_mean_diff": round(statistics.mean(diffs_all), 2),
        "paired_t": round(dt, 3) if dt is not None else None,
        "paired_p": round(dp, 4) if dp is not None else None,
    }
    print(f"\nPOOLED n={len(base_all)} base mean={bm:.1f} std={bs:.1f} sharpe={bm/bs:.3f} | "
          f"alt mean={am:.1f} std={as_:.1f} sharpe={am/as_:.3f} | "
          f"paired diff/day={statistics.mean(diffs_all):.2f} t={dt:.3f} p={dp:.4f}", flush=True)

    # single-day exclusion check (drop the single day with the largest |diff|)
    idx_max = max(range(len(diffs_all)), key=lambda i: abs(diffs_all[i]))
    win_x, day_x, b_x, a_x = day_rows_all[idx_max]
    base_ex = [v for i, v in enumerate(base_all) if i != idx_max]
    alt_ex = [v for i, v in enumerate(alt_all) if i != idx_max]
    bm_ex, bs_ex, *_ = one_sample_ttest(base_ex)
    am_ex, as_ex, *_ = one_sample_ttest(alt_ex)
    out["single_day_exclusion_check"] = {
        "excluded_day": day_x, "excluded_window": win_x,
        "base_net_that_day": b_x, "alt_net_that_day": a_x,
        "base_mean_ex": round(bm_ex, 1), "base_std_ex": round(bs_ex, 1),
        "alt_mean_ex": round(am_ex, 1), "alt_std_ex": round(as_ex, 1),
    }
    print(f"\nsingle-day check: excluding {day_x} ({win_x}, base={b_x} alt={a_x}) -> "
          f"base mean={bm_ex:.1f} std={bs_ex:.1f} | alt mean={am_ex:.1f} std={as_ex:.1f}", flush=True)

    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nsaved -> {OUT}")


if __name__ == "__main__":
    main()
