#!/usr/bin/env python3
"""Amplitude-filter overlay on 1-2m fade short-mom (research, read-only).

Task: test whether a volatility-regime-adaptive amplitude filter (sigma
z-score or trailing-day percentile-rank) on top of the already-validated
raw_mom / resid_mom **fade** signal (predicted direction = -sign(field))
improves OOS directed hit-rate vs the existing static 0.02% quiet-zone
baseline, without breaking the n>=1000 / name-stability gates.

Fade convention: this project's frozen result is that raw/resid short-mom
is *anti-predictive* as a continuation signal (OOS ~35-42%), i.e. it is a
**fade** signal: predicted direction = -sign(raw_pct or resid_pct). Directed
hit is computed against that fade prediction (matches "fade short-mom ~
58-65%" note in H3_RESIDUAL_SHORT_MOM.md).

Amplitude filter is defined on raw_pct_t (same field as
short_residual_mom_from_bars' raw_mom, mom_bars in {1,2}) and layered as an
AND-gate on top of BOTH the raw_mom and resid_mom fade signals (their own
sign definitions are unchanged):

  Design A (sigma z-score): sigma_t = stdev of trailing K 1-bar pct returns
    (same-day only, PIT: window ends at t-1). z_t = raw_pct_t / sigma_t.
    Trigger: |raw_pct_t| >= SHORT_MOM_EPS_PCT (static floor, unchanged)
             AND |z_t| >= k_sigma.
    Primary K=30; grid k_sigma in {1.0, 1.5, 2.0, 2.5}.

  Design B (trailing-day percentile rank): empirical distribution of
    |raw_pct_t| over the trailing D *full* trading days (PIT: only fully
    processed prior days, warm-up skipped). Trigger: |raw_pct_t| in the
    top P% of that distribution.
    Primary D=10; grid P in {30, 20, 10, 5}.

Same IS/OOS split, universe, and gate thresholds as
run_h3_residual_short_mom.py (7-name short-mom universe): OOS directed
hit >=55%, n>=1000, >=70% of names with n>=80 clearing >=52%, max single
name pooled-OOS share <=40%. IS-then-OOS discipline: champion filter is
picked on IS, then OOS is only used to *check* the gate (never to pick).

Example
-------
  PYTHONPATH=src .venv/bin/python \
    scripts/research/run_short_mom_fade_amplitude_filter.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from research.intraday_direction_thermometer import (  # noqa: E402
    SHORT_BETA_LOOKBACK,
    SHORT_MOM_EPS_PCT,
    SHORT_RESID_EPS_PCT,
)

import run_h3_residual_short_mom as h3  # noqa: E402  (reuse loaders/gates)

DEFAULT_STOCKS = ("2327", "8046", "3189", "6451", "2330", "2303", "2454")
BENCH_1M = "0050"
DEFAULT_START = "2024-01-02"
DEFAULT_IS_END = "2025-09-30"
FLAT_EPS = 0.0003
MIN_BARS_PER_DAY = 120
MIN_OOS_N = 1000
MIN_NAME_N = 80
NAME_HIT_FLOOR = 52.0
NAME_PASS_FRAC = 0.70
MAX_NAME_SHARE = 0.40
HIT_GATE = 55.0
HORIZONS = (5, 10, 15)
MOM_BARS_OPTIONS = (1, 2)
SKIP_OPEN_BARS = 5
BETA_LOOKBACK = SHORT_BETA_LOOKBACK
MOM_EPS_PCT = SHORT_MOM_EPS_PCT
RESID_EPS_PCT = SHORT_RESID_EPS_PCT

SIGMA_K_PRIMARY = 30
SIGMA_K_SENS = (20, 60)  # only applied to IS champion k_sigma afterwards
SIGMA_THRESHOLDS = (1.0, 1.5, 2.0, 2.5)

PCT_D_PRIMARY = 10
PCT_D_SENS = (5,)  # only applied to IS champion P afterwards
PCT_THRESHOLDS = (30, 20, 10, 5)


@dataclass(frozen=True)
class FilterSpec:
    name: str
    kind: str  # "baseline" | "sigma" | "pct"
    k: int | None = None
    thr: float | None = None  # k_sigma or P (percent)


def _primary_filters() -> tuple[FilterSpec, ...]:
    out = [FilterSpec("baseline", "baseline")]
    for k_sigma in SIGMA_THRESHOLDS:
        out.append(
            FilterSpec(f"sigma_{k_sigma}_K{SIGMA_K_PRIMARY}", "sigma", SIGMA_K_PRIMARY, k_sigma)
        )
    for p in PCT_THRESHOLDS:
        out.append(FilterSpec(f"pct_top{p}_D{PCT_D_PRIMARY}", "pct", PCT_D_PRIMARY, p))
    return tuple(out)


def _sensitivity_filters(champ_sigma: float | None, champ_pct: float | None) -> tuple[FilterSpec, ...]:
    out: list[FilterSpec] = []
    if champ_sigma is not None:
        for k in SIGMA_K_SENS:
            out.append(FilterSpec(f"sigma_{champ_sigma}_K{k}", "sigma", k, champ_sigma))
    if champ_pct is not None:
        for d in PCT_D_SENS:
            out.append(FilterSpec(f"pct_top{champ_pct}_D{d}", "pct", d, champ_pct))
    return tuple(out)


def _prefix_sums(vals: list[float]) -> tuple[list[float], list[float]]:
    n = len(vals)
    p1 = [0.0] * (n + 1)
    p2 = [0.0] * (n + 1)
    for i, v in enumerate(vals):
        p1[i + 1] = p1[i] + v
        p2[i + 1] = p2[i] + v * v
    return p1, p2


def _window_sigma(p1: list[float], p2: list[float], end_i: int, k: int) -> float | None:
    """stdev of s_rets[end_i-k : end_i] (k values, exclusive of end_i)."""
    if end_i - k < 0:
        return None
    s1 = p1[end_i] - p1[end_i - k]
    s2 = p2[end_i] - p2[end_i - k]
    mean = s1 / k
    var = s2 / k - mean * mean
    if var <= 0:
        return None
    return var**0.5


def eval_day(
    stock: list,
    bench: list,
    *,
    horizons: Sequence[int],
    beta_lookback: int,
    skip_open: int,
    pct_hist: dict[int, deque[list[float]]],  # mb -> deque of prior-day |raw_pct| arrays
    pct_d_needed: set[int],
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, list[float]]]:
    """Return (rows_by_mb, today_abs_raw_pct_by_mb).

    rows_by_mb[mb] = list of per-bar dicts:
      {i, raw_pct, resid_pct, fwd (dict H->ret), sigma: {K: val}, pct_rank_ok: {D: {P: bool}}}
    """
    keys_out: dict[int, list[dict[str, Any]]] = {mb: [] for mb in MOM_BARS_OPTIONS}
    today_abs: dict[int, list[float]] = {mb: [] for mb in MOM_BARS_OPTIONS}
    n = len(stock)
    if n != len(bench) or n < beta_lookback + 2:
        return keys_out, today_abs
    sc = [b.close for b in stock]
    bc = [b.close for b in bench]
    s_rets = [(sc[i] / sc[i - 1] - 1.0) if sc[i - 1] else 0.0 for i in range(1, n)]
    b_rets = [(bc[i] / bc[i - 1] - 1.0) if bc[i - 1] else 0.0 for i in range(1, n)]
    p1, p2 = _prefix_sums(s_rets)
    max_h = max(horizons)

    # Pre-build percentile thresholds per mb/D/P from history (fixed for whole day).
    pct_thresh: dict[int, dict[int, dict[int, float | None]]] = {}
    for mb in MOM_BARS_OPTIONS:
        pct_thresh[mb] = {}
        for d in pct_d_needed:
            hist = pct_hist[mb]
            if len(hist) < d:
                pct_thresh[mb][d] = {p: None for p in PCT_THRESHOLDS}
                continue
            combined: list[float] = []
            for day_arr in list(hist)[-d:]:
                combined.extend(day_arr)
            combined.sort()
            m = len(combined)
            thr_by_p: dict[int, float | None] = {}
            for p in PCT_THRESHOLDS:
                if m == 0:
                    thr_by_p[p] = None
                    continue
                # top P% => value at (100-P) percentile
                idx = int((1.0 - p / 100.0) * m)
                idx = min(max(idx, 0), m - 1)
                thr_by_p[p] = combined[idx]
            pct_thresh[mb][d] = thr_by_p

    i0 = max(skip_open, beta_lookback, max(MOM_BARS_OPTIONS))
    for i in range(i0, n - max_h):
        beta = h3._rolling_beta_at(s_rets, b_rets, end_i=i - 1, lookback=beta_lookback)
        if beta is None:
            continue
        sigmas: dict[int, float | None] = {}
        for k in set((SIGMA_K_PRIMARY,) + SIGMA_K_SENS):
            sigmas[k] = _window_sigma(p1, p2, i, k)
        c0 = sc[i]
        if not c0:
            continue
        fwd = {h: sc[i + h] / c0 - 1.0 for h in horizons}
        for mb in MOM_BARS_OPTIONS:
            if i - mb < 0:
                continue
            s0, b0 = sc[i - mb], bc[i - mb]
            if not s0 or not b0:
                continue
            raw_pct = 100.0 * (sc[i] / s0 - 1.0)
            mkt_pct = 100.0 * (bc[i] / b0 - 1.0)
            resid_pct = raw_pct - 100.0 * beta * (bc[i] / b0 - 1.0)
            today_abs[mb].append(abs(raw_pct))
            row = {
                "raw_pct": raw_pct,
                "resid_pct": resid_pct,
                "fwd": fwd,
                "sigma": sigmas,
                "pct_thresh": {d: pct_thresh[mb][d] for d in pct_d_needed},
            }
            keys_out[mb].append(row)
    return keys_out, today_abs


def _sign_fade(x: float, eps: float) -> int:
    """Fade prediction: -sign(x) if |x|>eps else 0."""
    if x > eps:
        return -1
    if x < -eps:
        return 1
    return 0


def _filter_pass(row: dict[str, Any], f: FilterSpec) -> bool:
    raw_abs = abs(row["raw_pct"])
    if raw_abs < MOM_EPS_PCT:
        return False
    if f.kind == "baseline":
        return True
    if f.kind == "sigma":
        sigma = row["sigma"].get(f.k)
        if sigma is None or sigma <= 0:
            return False
        z = raw_abs / sigma
        return z >= f.thr
    if f.kind == "pct":
        thr_map = row["pct_thresh"].get(f.k)
        if thr_map is None:
            return False
        thr = thr_map.get(int(f.thr))
        if thr is None:
            return False
        return raw_abs >= thr
    return False


def run(
    conn: sqlite3.Connection,
    *,
    stocks: Sequence[str],
    start: str,
    end: str | None,
    is_end: str,
    horizons: Sequence[int],
    filters: Sequence[FilterSpec],
) -> dict[str, Any]:
    end_s = end or "2099-12-31"
    pct_d_needed = {f.k for f in filters if f.kind == "pct"}
    print(f"loading bench {BENCH_1M} 1m ...", flush=True)
    bench_days = h3.load_days_1m(conn, BENCH_1M, start=start, end=end_s)
    print(f"  bench days={len(bench_days)}", flush=True)

    families = ("raw_mom", "resid_mom")
    variant_names = [
        f"{fam}_{mb}__{f.name}" for fam in families for mb in MOM_BARS_OPTIONS for f in filters
    ]
    pool: dict[str, dict[str, dict[int, list[dict[str, Any]]]]] = {
        vn: {"IS": {h: [] for h in horizons}, "OOS": {h: [] for h in horizons}}
        for vn in variant_names
    }
    per_stock: dict[str, dict[str, dict[str, dict[int, list[dict[str, Any]]]]]] = {}

    for sid in stocks:
        print(f"stock {sid} ...", flush=True)
        days = h3.load_days_1m(conn, sid, start=start, end=end_s)
        per_stock[sid] = {
            vn: {"IS": {h: [] for h in horizons}, "OOS": {h: [] for h in horizons}}
            for vn in variant_names
        }
        pct_hist: dict[int, deque[list[float]]] = {
            mb: deque(maxlen=max(PCT_D_PRIMARY, *PCT_D_SENS) if pct_d_needed else 1)
            for mb in MOM_BARS_OPTIONS
        }
        n_al = 0
        for td, sbars in sorted(days.items()):
            bbars = bench_days.get(td)
            if not bbars:
                continue
            s_al, b_al = h3.align_day(sbars, bbars)
            if len(s_al) < MIN_BARS_PER_DAY:
                continue
            n_al += 1
            split = "IS" if td <= is_end else "OOS"
            rows_by_mb, today_abs = eval_day(
                s_al,
                b_al,
                horizons=horizons,
                beta_lookback=BETA_LOOKBACK,
                skip_open=SKIP_OPEN_BARS,
                pct_hist=pct_hist,
                pct_d_needed=pct_d_needed if pct_d_needed else set(),
            )
            for mb in MOM_BARS_OPTIONS:
                for row in rows_by_mb[mb]:
                    sign_raw = _sign_fade(row["raw_pct"], MOM_EPS_PCT)
                    sign_resid = _sign_fade(row["resid_pct"], RESID_EPS_PCT)
                    for f in filters:
                        ok = _filter_pass(row, f)
                        if not ok:
                            continue
                        for fam, temp in (("raw_mom", sign_raw), ("resid_mom", sign_resid)):
                            if temp == 0:
                                continue
                            vn = f"{fam}_{mb}__{f.name}"
                            for h in horizons:
                                rec = {"temp": temp, "fwd": row["fwd"][h]}
                                pool[vn][split][h].append(rec)
                                per_stock[sid][vn][split][h].append(rec)
                # update trailing-day history AFTER this day's filters computed (PIT)
                if pct_d_needed:
                    pct_hist[mb].append(today_abs[mb])
        print(f"  aligned_days={n_al}", flush=True)

    leaderboard: list[dict[str, Any]] = []
    for vn in variant_names:
        for h in horizons:
            is_sum = h3.summarize(pool[vn]["IS"][h], flat_eps=FLAT_EPS)
            oos_sum = h3.summarize(pool[vn]["OOS"][h], flat_eps=FLAT_EPS)
            name_oos = {
                sid: h3.summarize(per_stock[sid][vn]["OOS"][h], flat_eps=FLAT_EPS)
                for sid in stocks
            }
            gates = h3.stability_gates(oos_sum, name_oos, hit_gate=HIT_GATE)
            leaderboard.append(
                {
                    "name": vn,
                    "horizon_m": h,
                    "IS": is_sum,
                    "OOS": oos_sum,
                    "gates": gates,
                }
            )
    return {
        "variants": variant_names,
        "horizons": list(horizons),
        "stocks": list(stocks),
        "leaderboard": leaderboard,
    }


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stocks", default=",".join(DEFAULT_STOCKS))
    ap.add_argument("--db", default=str(ROOT / "data" / "stocks.db"))
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=None)
    ap.add_argument("--is-end", default=DEFAULT_IS_END)
    ap.add_argument("--out-dir", type=Path, default=ROOT / "reports" / "research" / "intraday_direction_thermometer")
    ap.add_argument("--skip-sensitivity", action="store_true")
    args = ap.parse_args()
    stocks = tuple(s.strip() for s in args.stocks.split(",") if s.strip())

    conn = sqlite3.connect(args.db)
    try:
        primary_filters = _primary_filters()
        result = run(
            conn,
            stocks=stocks,
            start=args.start,
            end=args.end,
            is_end=args.is_end,
            horizons=HORIZONS,
            filters=primary_filters,
        )

        # ---- IS-champion selection (pooled IS hit, mom_bars-agnostic per family) ----
        # Champion sigma / pct picked on IS pooled hit (avg across mb & horizon),
        # restricted to cells that also meet the n>=1000-on-IS-scale sanity bar.
        def is_score(fname: str) -> tuple[float, int]:
            cells = [r for r in result["leaderboard"] if r["name"].split("__")[1] == fname]
            hits = [r["IS"]["directed_hit_pct"] for r in cells if r["IS"]["directed_hit_pct"] is not None]
            ns = [r["IS"]["n_directed"] or 0 for r in cells]
            avg_hit = sum(hits) / len(hits) if hits else -1.0
            return (avg_hit, sum(ns))

        sigma_names = [f.name for f in primary_filters if f.kind == "sigma"]
        pct_names = [f.name for f in primary_filters if f.kind == "pct"]
        champ_sigma_name = max(sigma_names, key=is_score) if sigma_names else None
        champ_pct_name = max(pct_names, key=is_score) if pct_names else None
        champ_sigma_val = None
        champ_pct_val = None
        if champ_sigma_name:
            champ_sigma_val = next(f.thr for f in primary_filters if f.name == champ_sigma_name)
        if champ_pct_name:
            champ_pct_val = next(f.thr for f in primary_filters if f.name == champ_pct_name)

        print(f"\nIS champion sigma filter: {champ_sigma_name} (score={is_score(champ_sigma_name) if champ_sigma_name else None})")
        print(f"IS champion pct filter:   {champ_pct_name} (score={is_score(champ_pct_name) if champ_pct_name else None})")

        sens_result = None
        if not args.skip_sensitivity and (champ_sigma_val is not None or champ_pct_val is not None):
            sens_filters = _sensitivity_filters(champ_sigma_val, champ_pct_val)
            if sens_filters:
                print(f"\nrunning sensitivity filters: {[f.name for f in sens_filters]}")
                sens_result = run(
                    conn,
                    stocks=stocks,
                    start=args.start,
                    end=args.end,
                    is_end=args.is_end,
                    horizons=HORIZONS,
                    filters=sens_filters,
                )
    finally:
        conn.close()

    all_lb = list(result["leaderboard"])
    if sens_result:
        all_lb.extend(sens_result["leaderboard"])

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "topic": "amplitude filter overlay on 1-2m fade short-mom",
        "metric": {
            "stocks": list(stocks),
            "start": args.start,
            "is_end": args.is_end,
            "bench": BENCH_1M,
            "horizons": list(HORIZONS),
            "mom_bars_options": list(MOM_BARS_OPTIONS),
            "mom_eps_pct": MOM_EPS_PCT,
            "resid_eps_pct": RESID_EPS_PCT,
            "beta_lookback": BETA_LOOKBACK,
            "hit_gate": HIT_GATE,
            "min_oos_n": MIN_OOS_N,
            "name_hit_floor": NAME_HIT_FLOOR,
            "name_pass_frac": NAME_PASS_FRAC,
            "max_name_share": MAX_NAME_SHARE,
        },
        "champion": {
            "sigma_filter": champ_sigma_name,
            "pct_filter": champ_pct_name,
        },
        "leaderboard": all_lb,
    }

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "short_mom_fade_amplitude_filter.json"
    slim_lb = []
    for r in sorted(
        all_lb,
        key=lambda r: (-(r["OOS"].get("directed_hit_pct") or -1), -(r["OOS"].get("n_directed") or 0)),
    ):
        slim = dict(r)
        pn = slim["gates"].get("per_name")
        slim = {**slim, "gates": {k: v for k, v in slim["gates"].items()}}
        slim_lb.append(slim)
    json_path.write_text(json.dumps({**out, "leaderboard": slim_lb}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {json_path}")

    print("\n=== OOS leaderboard (sorted by hit%, baseline + primary grid) ===")
    for r in sorted(
        result["leaderboard"],
        key=lambda r: (-(r["OOS"].get("directed_hit_pct") or -1), -(r["OOS"].get("n_directed") or 0)),
    ):
        o = r["OOS"]
        i = r["IS"]
        g = r["gates"]
        print(
            f"  {r['name']:38s} H={r['horizon_m']:2d} OOS_hit={o.get('directed_hit_pct')!s:>6} "
            f"n={o.get('n_directed')!s:>7} IS_hit={i.get('directed_hit_pct')!s:>6} "
            f"nameFrac={g['name_floor_frac']} maxShare={g['max_name_share']} "
            f"gates={'PASS' if g['all_gates_pass'] else 'fail'}"
        )

    if sens_result:
        print("\n=== OOS leaderboard (sensitivity K/D grid) ===")
        for r in sorted(
            sens_result["leaderboard"],
            key=lambda r: (-(r["OOS"].get("directed_hit_pct") or -1), -(r["OOS"].get("n_directed") or 0)),
        ):
            o = r["OOS"]
            g = r["gates"]
            print(
                f"  {r['name']:38s} H={r['horizon_m']:2d} OOS_hit={o.get('directed_hit_pct')!s:>6} "
                f"n={o.get('n_directed')!s:>7} gates={'PASS' if g['all_gates_pass'] else 'fail'}"
            )

    any_pass = any(r["gates"]["all_gates_pass"] for r in all_lb)
    baseline_cells = [r for r in result["leaderboard"] if r["name"].endswith("__baseline")]
    print(f"\nANY_GATE_PASS={any_pass}")
    print("baseline (no filter) reference cells:")
    for r in sorted(baseline_cells, key=lambda r: (r["name"], r["horizon_m"])):
        o = r["OOS"]
        print(f"  {r['name']:26s} H={r['horizon_m']:2d} OOS_hit={o.get('directed_hit_pct')} n={o.get('n_directed')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
