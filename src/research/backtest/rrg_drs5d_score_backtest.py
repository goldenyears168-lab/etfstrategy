"""RRG 5-day rightward displacement (drs_5d) scoring backtest · PIT pre-release pool.

Research question: signal-day kinematic features at D → drs_5d = RSR(D+5) − RSR(D).
Hold 5 trading days · sell at D+5 close.
Auxiliary: flip_leading_5d · cross_100_5d · ret_5d (price · not optimized by score).
"""

from __future__ import annotations

import itertools
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.backtest.rrg_drs3d_score_backtest import (
    DEFAULT_KINEMATIC_WEIGHTS,
    DEFAULT_WEIGHTS,
    ScoreABComparison,
    _attach_pit_trajectory_features,
    _spearman_pvalue,
    _years_span,
    attach_horizon_outcomes,
    compute_drs3d_score,
    compute_drs3d_score_kinematic,
    cross_sectional_percentile_rank,
    evaluate_score_ab_comparison,
    evaluate_score_deciles,
    pool_omega_kinematic_mask,
    pool_omega_mask,
)
from research.backtest.rrg_pre_release_funnel_lanes_3d import build_pre_release_funnel_panel_3d
from research.backtest.tw100_walk_forward import _ic_stats
from rank_stats import spearman_correlation
from rrg_rotation import DEFAULT_LENGTH
from stock_db import PROJECT_ROOT

HOLD_DAYS = 5

# 5-day horizon · velocity scale tuned for larger drs_5d dispersion
DEFAULT_WEIGHTS_5D: dict[str, float] = {
    **DEFAULT_WEIGHTS,
    "w_proximity": 0.8,
    "w_velocity": 0.65,
    "w_spread": 0.25,
    "w_mono": 0.35,
    "vel_scale": 3.0,
    "spread_scale": 1.2,
}

DEFAULT_KINEMATIC_WEIGHTS_5D: dict[str, float] = {
    **DEFAULT_KINEMATIC_WEIGHTS,
    # Grid-tuned on full-sample Ω′ (superseded OOS by walk-forward each fold)
    "w_velocity": 0.95,
    "w_heading": 0.35,
    "w_spread": 0.35,
    "w_mono": 0.25,
    "w_accel": 0.15,
    "vel_scale": 3.0,
    "spread_scale": 1.2,
}

# In-sample grid best (reference · re-fit each WFA fold on train only)
TUNED_KINEMATIC_WEIGHTS_5D: dict[str, float] = dict(DEFAULT_KINEMATIC_WEIGHTS_5D)

TRAJECTORY_FEATURES: tuple[tuple[str, str], ...] = (
    ("drs_rsr_5d", "5d RS-Ratio Δ (signal velocity)"),
    ("drs_rsr_4d", "4d RS-Ratio Δ"),
    ("v20", "structural velocity v20"),
    ("heading_rsr", "rightward purity dr/disp"),
    ("drs_rsr_1d", "1d RS-Ratio Δ"),
    ("a_step", "step acceleration seg_last−mean"),
    ("disp", "4d Euclidean displacement"),
)

WF_TRAIN_YEARS = 3
WF_TEST_YEARS = 1
WF_MIN_CROSS_SECTION = 8


def build_year_walk_forward_folds(
    trading_dates: list[str],
    *,
    train_years: int = WF_TRAIN_YEARS,
    test_years: int = WF_TEST_YEARS,
) -> list[dict[str, Any]]:
    """Rolling year WFA · train N years → test next M years."""
    if not trading_dates:
        return []
    dates = sorted(set(trading_dates))
    years = sorted({d[:4] for d in dates})
    folds: list[dict[str, Any]] = []
    fold_id = 0
    for i in range(len(years) - train_years - test_years + 1):
        train_y = years[i : i + train_years]
        test_y = years[i + train_years : i + train_years + test_years]
        train_set, test_set = set(train_y), set(test_y)
        train_dates = [d for d in dates if d[:4] in train_set]
        test_dates = [d for d in dates if d[:4] in test_set]
        if not train_dates or not test_dates:
            continue
        folds.append(
            {
                "fold_id": fold_id,
                "train_start": train_dates[0],
                "train_end": train_dates[-1],
                "test_start": test_dates[0],
                "test_end": test_dates[-1],
                "train_years": tuple(train_y),
                "test_years": tuple(test_y),
            }
        )
        fold_id += 1
    return folds

_OUTCOME_SUFFIX = "5d"
_DRS_COL = f"drs_{_OUTCOME_SUFFIX}"
_RET_COL = f"ret_{_OUTCOME_SUFFIX}"
_FLIP_COL = f"flip_leading_{_OUTCOME_SUFFIX}"
_CROSS_COL = f"cross_100_{_OUTCOME_SUFFIX}"


def compute_drs5d_score(
    row: pd.Series | dict[str, Any],
    *,
    weights: dict[str, float] | None = None,
) -> float:
    """Position-aware score tuned for 5-day structural rightward displacement."""
    w = weights or DEFAULT_WEIGHTS_5D
    if isinstance(row, pd.Series):
        row = row.to_dict()

    vel_scale = w.get("vel_scale", 3.0)
    spread_scale = w.get("spread_scale", 1.2)
    dr5 = float(row.get("drs_rsr_5d", row.get("drs_rsr_4d", float("nan"))))
    patched = {**row, "drs_rsr_4d": dr5}
    base_w = {**w, "w_velocity": w["w_velocity"]}
    raw = compute_drs3d_score(patched, weights=base_w)
    # Re-scale velocity term for 5d magnitude
    if np.isfinite(dr5) and dr5 > 0:
        vel_bonus = w["w_velocity"] * (min(max(dr5, 0.0) / vel_scale, 1.0) - min(max(dr5 / 4, 0.0) / 2.0, 1.0))
        dr1 = float(row.get("drs_rsr_1d", float("nan")))
        spread_bonus = w["w_spread"] * (min(max(dr1, 0.0) / spread_scale, 1.0) - min(max(dr1, 0.0), 1.0))
        raw += vel_bonus * 0.5 + spread_bonus * 0.3
    return round(max(raw, 0.0), 6)


def compute_drs5d_score_kinematic(
    row: pd.Series | dict[str, Any],
    *,
    weights: dict[str, float] | None = None,
) -> float:
    """Motion-first score for 5-day drs_5d · uses v20 from 5-day dr when available."""
    w = weights or DEFAULT_KINEMATIC_WEIGHTS_5D
    if isinstance(row, pd.Series):
        row = row.to_dict()

    dr5 = float(row.get("drs_rsr_5d", row.get("drs_rsr_4d", float("nan"))))
    v5 = dr5 / 5.0 if np.isfinite(dr5) else float(row.get("v20", float("nan")))
    patched = {**row, "v20": v5, "drs_rsr_4d": dr5}
    return compute_drs3d_score_kinematic(patched, weights=w)


def _attach_5d_velocity(panel: pd.DataFrame, conn: Any) -> pd.DataFrame:
    """Add drs_rsr_5d = RSR(D) − RSR(D−4) · 5-point span velocity at D."""
    from market_benchmark import load_benchmark_close
    from research.backtest.finpilot_local_backtest import load_price_panels
    from rrg_rotation import compute_rrg_panel

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn)
    rs_ratio, _, _ = compute_rrg_panel(close, bench, length=DEFAULT_LENGTH)
    full_dates = close.index.astype(str).tolist()
    date_idx = {d: i for i, d in enumerate(full_dates)}

    dr5s: list[float] = []
    for _, row in panel.iterrows():
        d, sid = str(row["date"]), str(row["sid"])
        si = date_idx.get(d)
        if si is None or si < 4 or sid not in rs_ratio.columns:
            dr5s.append(float("nan"))
            continue
        d0 = full_dates[si - 4]
        rv0 = float(rs_ratio.at[d0, sid])
        rv1 = float(rs_ratio.at[d, sid])
        dr5s.append(rv1 - rv0 if np.isfinite(rv0) and np.isfinite(rv1) else float("nan"))

    out = panel.copy()
    out["drs_rsr_5d"] = dr5s
    return out


def build_drs5d_panel(
    conn: Any | None = None,
    *,
    events: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Pre-release pool · PIT kinematics at D · drs_5d outcomes · scores."""
    conn = conn or __import__("stock_db", fromlist=["connect"]).connect()
    if events is not None:
        panel, meta = build_pre_release_funnel_panel_3d(events=events)
    else:
        panel, meta = build_pre_release_funnel_panel_3d(conn, events=events)

    if panel.empty:
        return panel, {**meta, "omega_n": 0, "hold_days": HOLD_DAYS}

    panel = _attach_pit_trajectory_features(panel, conn)
    panel = attach_horizon_outcomes(panel, conn, hold_days=HOLD_DAYS)
    panel = _attach_5d_velocity(panel, conn)

    if "rsr_d" not in panel.columns and "rv" in panel.columns:
        panel["rsr_d"] = panel["rv"]

    panel = panel.copy()
    panel["drs5d_score"] = panel.apply(compute_drs5d_score, axis=1)
    panel["drs5d_score_kin_raw"] = panel.apply(compute_drs5d_score_kinematic, axis=1)
    panel["in_pool_omega"] = pool_omega_mask(panel)
    panel["in_pool_omega_kinematic"] = pool_omega_kinematic_mask(panel)
    panel["drs5d_score_kin"] = cross_sectional_percentile_rank(
        panel, "drs5d_score_kin_raw", mask_col="in_pool_omega_kinematic"
    )

    meta = {
        **meta,
        "omega_n": int(panel["in_pool_omega"].sum()),
        "omega_kinematic_n": int(panel["in_pool_omega_kinematic"].sum()),
        "structural_outcome": _DRS_COL,
        "hold_days": HOLD_DAYS,
        "score_weights": DEFAULT_WEIGHTS_5D,
        "kinematic_weights": DEFAULT_KINEMATIC_WEIGHTS_5D,
    }
    return panel, meta


def evaluate_score_ab_comparison_5d(panel: pd.DataFrame) -> ScoreABComparison:
    """A/B on drs_5d outcome."""
    kw = {
        "drs_col": _DRS_COL,
        "ret_col": _RET_COL,
        "flip_col": _FLIP_COL,
        "cross_col": _CROSS_COL,
    }
    position = evaluate_score_deciles(
        panel, subset_col="in_pool_omega", score_col="drs5d_score", **kw
    )
    kinematic = evaluate_score_deciles(
        panel, subset_col="in_pool_omega_kinematic", score_col="drs5d_score_kin", **kw
    )
    return ScoreABComparison(
        position=position,
        kinematic=kinematic,
        kinematic_with_proximity=None,
        h2_kinematic_ic_near_zero=kinematic.h0_ic_near_zero,
    )


def sweep_kinematic_weights_5d(
    panel: pd.DataFrame,
    *,
    subset_col: str = "in_pool_omega_kinematic",
) -> dict[str, Any]:
    """Grid search kinematic weights · maximize Spearman(score, drs_5d) on Ω′."""
    sub = panel[panel[subset_col]].copy() if subset_col in panel.columns else panel.copy()
    sub = sub.dropna(subset=[_DRS_COL])
    if len(sub) < 100:
        return {"best_weights": DEFAULT_KINEMATIC_WEIGHTS_5D, "best_ic": 0.0, "n_trials": 0}

    grid_vel = [0.55, 0.75, 0.95]
    grid_head = [0.35, 0.55, 0.75]
    grid_mono = [0.25, 0.45]
    grid_spread = [0.15, 0.35]

    best_ic = -1.0
    best_w = dict(DEFAULT_KINEMATIC_WEIGHTS_5D)
    trials = 0

    for wv, wh, wm, ws in itertools.product(grid_vel, grid_head, grid_mono, grid_spread):
        w = {**DEFAULT_KINEMATIC_WEIGHTS_5D, "w_velocity": wv, "w_heading": wh, "w_mono": wm, "w_spread": ws}
        scores = sub.apply(lambda r: compute_drs5d_score_kinematic(r, weights=w), axis=1)
        ic_raw = spearman_correlation(scores.tolist(), sub[_DRS_COL].tolist())
        ic = float(ic_raw) if ic_raw is not None else 0.0
        trials += 1
        if ic > best_ic:
            best_ic = ic
            best_w = w

    return {
        "best_weights": best_w,
        "best_ic": round(best_ic, 4),
        "n_trials": trials,
        "default_ic": _ic_kinematic_default(sub),
    }


def _ic_kinematic_default(sub: pd.DataFrame) -> float:
    scores = sub.apply(compute_drs5d_score_kinematic, axis=1)
    ic_raw = spearman_correlation(scores.tolist(), sub[_DRS_COL].tolist())
    return round(float(ic_raw), 4) if ic_raw is not None else 0.0


def daily_spearman_ic_series(
    df: pd.DataFrame,
    *,
    score_col: str,
    label_col: str = _DRS_COL,
    min_cross_section: int = WF_MIN_CROSS_SECTION,
) -> list[tuple[str, float]]:
    """Per-date cross-sectional Spearman IC(score, drs_5d)."""
    out: list[tuple[str, float]] = []
    for dt, grp in df.groupby("date"):
        sub = grp[[score_col, label_col]].dropna()
        if len(sub) < min_cross_section:
            continue
        ic = spearman_correlation(sub[score_col].tolist(), sub[label_col].tolist())
        if ic is not None:
            out.append((str(dt), float(ic)))
    return out


def pooled_spearman_ic(
    df: pd.DataFrame,
    *,
    score_col: str,
    label_col: str = _DRS_COL,
) -> tuple[float, float, int]:
    """Pooled Spearman IC · p-value · n."""
    sub = df[[score_col, label_col]].dropna()
    n = len(sub)
    if n < 3:
        return 0.0, 1.0, n
    ic_raw = spearman_correlation(sub[score_col].tolist(), sub[label_col].tolist())
    ic = float(ic_raw) if ic_raw is not None else 0.0
    return round(ic, 4), round(_spearman_pvalue(ic, n), 6), n


def _apply_kinematic_scores(
    df: pd.DataFrame,
    weights: dict[str, float],
    *,
    raw_col: str = "drs5d_score_kin_raw",
    score_col: str = "drs5d_score_kin",
    mask_col: str = "in_pool_omega_kinematic",
) -> pd.DataFrame:
    out = df.copy()
    out[raw_col] = out.apply(lambda r: compute_drs5d_score_kinematic(r, weights=weights), axis=1)
    out[score_col] = cross_sectional_percentile_rank(out, raw_col, mask_col=mask_col)
    return out


def analyze_trajectory_feature_ic(
    panel: pd.DataFrame,
    *,
    subset_col: str = "in_pool_omega_kinematic",
) -> dict[str, Any]:
    """Per-feature pooled + daily IC vs drs_5d · trajectory differential predictive power."""
    sub = panel[panel[subset_col]].copy() if subset_col in panel.columns else panel.copy()
    sub = sub.dropna(subset=[_DRS_COL])
    rows: list[dict[str, Any]] = []

    for feat, label in TRAJECTORY_FEATURES:
        if feat not in sub.columns:
            continue
        feat_df = sub[[feat, _DRS_COL, "date"]].dropna(subset=[feat])
        if len(feat_df) < 30:
            continue
        ic, p, n = pooled_spearman_ic(feat_df, score_col=feat, label_col=_DRS_COL)
        daily = daily_spearman_ic_series(feat_df, score_col=feat, label_col=_DRS_COL)
        daily_ics = [v for _, v in daily]
        dstats = _ic_stats(daily_ics)
        rows.append(
            {
                "feature": feat,
                "label": label,
                "pooled_ic": ic,
                "pooled_p": p,
                "n": n,
                "daily_ic_mean": dstats.get("mean"),
                "daily_ic_icir": dstats.get("icir"),
                "daily_ic_n_days": dstats.get("n_days"),
                "significant_p05": p < 0.05 and ic > 0,
            }
        )

    # mono_up as binary feature
    if "mono_up" in sub.columns:
        mono_df = sub.assign(_mono=sub["mono_up"].astype(float))
        ic, p, n = pooled_spearman_ic(mono_df, score_col="_mono", label_col=_DRS_COL)
        daily = daily_spearman_ic_series(mono_df, score_col="_mono", label_col=_DRS_COL)
        daily_ics = [v for _, v in daily]
        dstats = _ic_stats(daily_ics)
        rows.append(
            {
                "feature": "mono_up",
                "label": "monotonic step acceleration",
                "pooled_ic": ic,
                "pooled_p": p,
                "n": n,
                "daily_ic_mean": dstats.get("mean"),
                "daily_ic_icir": dstats.get("icir"),
                "daily_ic_n_days": dstats.get("n_days"),
                "significant_p05": p < 0.05 and ic > 0,
            }
        )

    rows.sort(key=lambda r: -(r["pooled_ic"] or 0))
    n_sig = sum(1 for r in rows if r.get("significant_p05"))
    return {
        "features": rows,
        "n_features_tested": len(rows),
        "n_significant_p05": n_sig,
        "verdict": _trajectory_verdict(rows),
    }


def _trajectory_verdict(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "insufficient_data"
    top = rows[0]
    if top.get("pooled_ic", 0) > 0.05 and top.get("pooled_p", 1) < 0.05:
        if sum(1 for r in rows if r.get("significant_p05")) >= 2:
            return "yes_multiple_features"
        return "yes_top_feature_only"
    if any(r.get("significant_p05") for r in rows):
        return "weak_some_features"
    return "not_significant_pooled"


def run_walk_forward_validation(
    panel: pd.DataFrame,
    *,
    subset_col: str = "in_pool_omega_kinematic",
    train_years: int = WF_TRAIN_YEARS,
    test_years: int = WF_TEST_YEARS,
) -> dict[str, Any]:
    """Rolling year WFA: tune kinematic weights on train Ω′ · evaluate OOS drs_5d IC."""
    sub = panel[panel[subset_col]].copy() if subset_col in panel.columns else panel.copy()
    sub = sub.dropna(subset=[_DRS_COL])
    if sub.empty:
        return {"folds": [], "summary": {}, "passed": False}

    dates = sorted(sub["date"].astype(str).unique().tolist())
    folds = build_year_walk_forward_folds(
        dates, train_years=train_years, test_years=test_years
    )
    fold_rows: list[dict[str, Any]] = []
    oos_pooled_ics: list[float] = []
    oos_daily_ics: list[float] = []

    for fold in folds:
        train = sub[sub["date"].astype(str).between(fold["train_start"], fold["train_end"])]
        test = sub[sub["date"].astype(str).between(fold["test_start"], fold["test_end"])]
        if len(train) < 80 or len(test) < 30:
            continue

        sweep = sweep_kinematic_weights_5d(train, subset_col=None)
        tuned_w = sweep.get("best_weights", TUNED_KINEMATIC_WEIGHTS_5D)
        train_ic = sweep.get("best_ic", 0.0)

        test_scored = _apply_kinematic_scores(test, tuned_w)
        oos_ic, oos_p, oos_n = pooled_spearman_ic(
            test_scored, score_col="drs5d_score_kin", label_col=_DRS_COL
        )
        daily = daily_spearman_ic_series(test_scored, score_col="drs5d_score_kin", label_col=_DRS_COL)
        daily_vals = [v for _, v in daily]
        dstats = _ic_stats(daily_vals)
        oos_pooled_ics.append(oos_ic)
        oos_daily_ics.extend(daily_vals)

        # Default weights OOS for comparison
        test_default = _apply_kinematic_scores(test, DEFAULT_KINEMATIC_WEIGHTS_5D)
        def_ic, _, _ = pooled_spearman_ic(test_default, score_col="drs5d_score_kin", label_col=_DRS_COL)

        fold_rows.append(
            {
                "fold_id": fold["fold_id"],
                "train_years": list(fold["train_years"]),
                "test_years": list(fold["test_years"]),
                "train_n": len(train),
                "test_n": oos_n,
                "train_ic_in_sample": train_ic,
                "oos_ic_tuned": oos_ic,
                "oos_p_tuned": oos_p,
                "oos_ic_default": def_ic,
                "daily_ic_mean": dstats.get("mean"),
                "daily_ic_n_days": dstats.get("n_days"),
                "tuned_weights": {
                    k: tuned_w[k]
                    for k in ("w_velocity", "w_heading", "w_mono", "w_spread")
                    if k in tuned_w
                },
            }
        )

    n_folds = len(fold_rows)
    n_pos = sum(1 for ic in oos_pooled_ics if ic > 0)
    mean_oos = float(np.mean(oos_pooled_ics)) if oos_pooled_ics else 0.0
    daily_stats = _ic_stats(oos_daily_ics)
    passed = n_folds >= 4 and n_pos >= max(4, int(n_folds * 0.625)) and mean_oos > 0.03

    return {
        "folds": fold_rows,
        "summary": {
            "n_folds": n_folds,
            "n_oos_ic_positive": n_pos,
            "mean_oos_ic_tuned": round(mean_oos, 4),
            "median_oos_ic_tuned": round(float(np.median(oos_pooled_ics)), 4) if oos_pooled_ics else 0.0,
            "oos_daily_ic_mean": daily_stats.get("mean"),
            "oos_daily_ic_icir": daily_stats.get("icir"),
            "oos_daily_ic_n_days": daily_stats.get("n_days"),
            "train_years": train_years,
            "test_years": test_years,
        },
        "passed": passed,
        "verdict": (
            "pass"
            if passed
            else ("marginal" if mean_oos > 0 and n_pos >= n_folds // 2 else "fail")
        ),
    }


def render_drs5d_markdown(result: dict[str, Any]) -> str:
    meta = result["meta"]
    ev = result["evaluation"]
    ab = result.get("ab_comparison", {})
    sweep = result.get("weight_sweep", {})
    w = result.get("weights", DEFAULT_WEIGHTS_5D)
    kw = result.get("kinematic_weights", DEFAULT_KINEMATIC_WEIGHTS_5D)
    bl = result.get("pool_omega_baseline", {})
    blk = result.get("pool_omega_kinematic_baseline", {})

    lines = [
        "# RRG drs_5d displacement score · 5-day hold backtest",
        "",
        f"> **Primary outcome**: `drs_5d = RSR(D+5) − RSR(D)` · WMA({meta.get('rrg_length', DEFAULT_LENGTH)}) RS-Ratio",
        f"> **Research**: signal-day kinematics (D) → 5-day structural rightward shift · sell at D+5 close",
        f"> **Auxiliary**: `flip_leading_5d` · `cross_100_5d` · `ret_5d`",
        "",
        f"- Pre-release pool: **{meta.get('pool_n', ev['pool_n'])}** stock-days",
        f"- Pool Ω: **{ev['omega_n']}** · Pool Ω′: **{meta.get('omega_kinematic_n', '—')}**",
        f"- Span: {meta.get('date_start', '')} → {meta.get('date_end', '')} · Generated: {meta.get('generated_on', '')}",
        "",
        "## A. Structural outcome (Pool Ω)",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Spearman IC (score vs **drs_5d**) | **{ev['spearman_ic']:+.4f}** (p={ev['spearman_p']:.4f}) |",
        f"| Pool mean drs_5d | {ev['pool_mean_drs_3d']:+.3f} |",
        f"| Top decile mean drs_5d | **{ev['top_decile_mean_drs_3d']:+.3f}** |",
        f"| Top decile lift | **{ev['top_decile_lift']:+.3f}** |",
        f"| Pool mean **ret_5d** | {bl.get('mean_ret_5d', 0):+.2f}% |",
        f"| Top decile mean ret_5d | {ev['deciles'][-1]['mean_ret_3d']:+.2f}% |" if ev.get("deciles") else "",
        f"| Pool P(flip→Leading@D+5) | {ev['pool_flip_rate']:.1%} |",
        f"| Top decile P(flip) | {ev['top_decile_flip_rate']:.1%} |",
        f"| Pool P(cross_100_5d) | {ev.get('pool_cross_100_rate', 0):.1%} |",
        f"| Top decile P(cross_100) | {ev.get('top_decile_cross_100_rate', 0):.1%} |",
    ]

    if ab:
        kin = ab.get("kinematic", {})
        lines.extend(
            [
                "",
                "## A′. Kinematic A/B (Ω vs Ω′ on drs_5d)",
                "",
                "| Metric | Ω position | Ω′ kinematic |",
                "|--------|------------|--------------|",
                f"| n | {ev['omega_n']} | {kin.get('omega_n', 0)} |",
                f"| IC (score vs drs_5d) | {ev['spearman_ic']:+.4f} | **{kin.get('spearman_ic', 0):+.4f}** |",
                f"| Top decile lift | {ev['top_decile_lift']:+.3f} | {kin.get('top_decile_lift', 0):+.3f} |",
                f"| Top decile P(flip) | {ev['top_decile_flip_rate']:.1%} | {kin.get('top_decile_flip_rate', 0):.1%} |",
                f"| Ω′ mean ret_5d | {bl.get('mean_ret_5d', 0):+.2f}% | {blk.get('mean_ret_5d', 0):+.2f}% |",
            ]
        )

    if sweep:
        bw = sweep.get("best_weights", {})
        lines.extend(
            [
                "",
                "## B. Weight sweep (Ω′ · maximize IC vs drs_5d)",
                "",
                f"- Trials: **{sweep.get('n_trials', 0)}**",
                f"- Default kinematic IC: **{sweep.get('default_ic', 0):+.4f}**",
                f"- Best grid IC: **{sweep.get('best_ic', 0):+.4f}**",
                f"- Best: w_velocity={bw.get('w_velocity')} · w_heading={bw.get('w_heading')} · "
                f"w_mono={bw.get('w_mono')} · w_spread={bw.get('w_spread')}",
            ]
        )

    feat = result.get("trajectory_feature_ic", {})
    if feat.get("features"):
        lines.extend(
            [
                "",
                "## D. Trajectory feature IC vs drs_5d (Ω′ pooled)",
                "",
                f"- Features tested: **{feat.get('n_features_tested', 0)}** · "
                f"significant (p<0.05, IC>0): **{feat.get('n_significant_p05', 0)}**",
                f"- **Verdict**: `{feat.get('verdict', '')}`",
                "",
                "| Feature | pooled IC | p | daily IC mean | ICIR | sig? |",
                "|---------|-----------|---|---------------|------|------|",
            ]
        )
        for r in feat["features"]:
            sig = "✓" if r.get("significant_p05") else "—"
            lines.append(
                f"| {r['feature']} | {r['pooled_ic']:+.4f} | {r['pooled_p']:.4f} | "
                f"{r.get('daily_ic_mean') or 0:+.4f} | {r.get('daily_ic_icir') or '—'} | {sig} |"
            )

    wf = result.get("walk_forward", {})
    if wf.get("folds"):
        sm = wf.get("summary", {})
        lines.extend(
            [
                "",
                "## E. Walk-forward validation (Ω′ · tune on train · OOS test)",
                "",
                f"- Folds: **{sm.get('n_folds', 0)}** ({sm.get('train_years', 3)}y train / "
                f"{sm.get('test_years', 1)}y test)",
                f"- OOS IC>0 folds: **{sm.get('n_oos_ic_positive', 0)}/{sm.get('n_folds', 0)}**",
                f"- Mean OOS IC (tuned): **{sm.get('mean_oos_ic_tuned', 0):+.4f}**",
                f"- OOS daily IC mean: **{sm.get('oos_daily_ic_mean') or 0:+.4f}** · "
                f"ICIR={sm.get('oos_daily_ic_icir') or '—'}",
                f"- **WFA passed?** {'✅' if wf.get('passed') else '❌'} · `{wf.get('verdict', '')}`",
                "",
                "| Fold | train | test | IS IC | OOS IC tuned | OOS IC default |",
                "|------|-------|------|-------|--------------|----------------|",
            ]
        )
        for f in wf["folds"]:
            lines.append(
                f"| {f['fold_id']} | {','.join(f['train_years'])} | {','.join(f['test_years'])} | "
                f"{f['train_ic_in_sample']:+.3f} | **{f['oos_ic_tuned']:+.3f}** | "
                f"{f['oos_ic_default']:+.3f} |"
            )

    lines.extend(
        [
            "",
            "## F. Decile table (Pool Ω · drs_5d)",
            "",
            "| D | n | mean drs_5d | mean ret_5d | P(flip) | P(cross_100) |",
            "|---|---|-------------|-------------|---------|--------------|",
        ]
    )
    for d in ev.get("deciles", []):
        lines.append(
            f"| {d['decile']} | {d['n']} | {d['mean_drs_3d']:+.3f} | {d['mean_ret_3d']:+.2f}% | "
            f"{d['flip_leading_rate']:.1%} | — |"
        )

    lines.extend(
        [
            "",
            "## G. Research checklist (5-day structural hold)",
            "",
            "1. **Y = drs_5d** (primary) · ret_5d auxiliary only",
            "2. **PIT features at D**: v20/v5 · heading_rsr · mono_up · disp · proximity",
            "3. **Pool Ω or Ω′** → rank by score → validate IC · decile lift · P(flip/cross_100)",
            "4. **Tune** weights on train fold only · validate OOS (section E)",
            "5. **Feature IC** (section D) confirms which trajectory differentials carry signal",
            "",
            "## H. PIT checklist",
            "",
            "- [x] Features at D close only",
            "- [x] drs_5d / flip_leading_5d are D+5 outcomes",
            "- [x] Hold = 5 trading days · exit D+5 close",
        ]
    )
    return "\n".join(lines)


def run_drs5d_score_backtest(conn: Any | None = None) -> dict[str, Any]:
    conn = conn or __import__("stock_db", fromlist=["connect"]).connect()
    panel, meta = build_drs5d_panel(conn)
    omega = panel[panel["in_pool_omega"]] if "in_pool_omega" in panel.columns else panel
    omega_kin = (
        panel[panel["in_pool_omega_kinematic"]] if "in_pool_omega_kinematic" in panel.columns else panel
    )

    kw = {
        "drs_col": _DRS_COL,
        "ret_col": _RET_COL,
        "flip_col": _FLIP_COL,
        "cross_col": _CROSS_COL,
    }
    evaluation = evaluate_score_deciles(
        panel, subset_col="in_pool_omega", score_col="drs5d_score", **kw
    )
    ab = evaluate_score_ab_comparison_5d(panel)
    sweep = sweep_kinematic_weights_5d(panel)
    trajectory_ic = analyze_trajectory_feature_ic(panel)
    walk_forward = run_walk_forward_validation(panel)

    # Re-score with swept weights for tuned evaluation
    tuned_w = sweep.get("best_weights", TUNED_KINEMATIC_WEIGHTS_5D)
    panel_tuned = panel.copy()
    panel_tuned["drs5d_score_kin_tuned_raw"] = panel_tuned.apply(
        lambda r: compute_drs5d_score_kinematic(r, weights=tuned_w), axis=1
    )
    panel_tuned["drs5d_score_kin_tuned"] = cross_sectional_percentile_rank(
        panel_tuned, "drs5d_score_kin_tuned_raw", mask_col="in_pool_omega_kinematic"
    )
    tuned_eval = evaluate_score_deciles(
        panel_tuned,
        subset_col="in_pool_omega_kinematic",
        score_col="drs5d_score_kin_tuned",
        **kw,
    )

    years = _years_span(panel["date"]) if len(panel) else 1.0
    return {
        "meta": {
            **meta,
            "generated_on": date.today().isoformat(),
            "pool_n": len(panel),
            "years_span": round(years, 3),
            "date_start": str(panel["date"].min()) if len(panel) else "",
            "date_end": str(panel["date"].max()) if len(panel) else "",
            "hold_days": HOLD_DAYS,
            "rrg_length": DEFAULT_LENGTH,
        },
        "weights": DEFAULT_WEIGHTS_5D,
        "kinematic_weights": DEFAULT_KINEMATIC_WEIGHTS_5D,
        "evaluation": asdict(evaluation),
        "ab_comparison": {
            "position": asdict(ab.position),
            "kinematic": asdict(ab.kinematic),
            "h2_kinematic_ic_near_zero": ab.h2_kinematic_ic_near_zero,
        },
        "tuned_kinematic_evaluation": asdict(tuned_eval),
        "weight_sweep": sweep,
        "trajectory_feature_ic": trajectory_ic,
        "walk_forward": walk_forward,
        "pool_omega_baseline": _pool_baseline(omega),
        "pool_omega_kinematic_baseline": _pool_baseline(omega_kin),
    }


def _pool_baseline(sub: pd.DataFrame) -> dict[str, float | int]:
    if sub.empty:
        return {"n": 0, "mean_drs_5d": 0.0, "mean_ret_5d": 0.0, "flip_leading_5d_rate": 0.0, "cross_100_5d_rate": 0.0}
    return {
        "n": len(sub),
        "mean_drs_5d": round(float(sub[_DRS_COL].mean()), 4),
        "mean_ret_5d": round(float(sub[_RET_COL].mean()), 4),
        "flip_leading_5d_rate": round(float(sub[_FLIP_COL].mean()), 4)
        if _FLIP_COL in sub.columns
        else 0.0,
        "cross_100_5d_rate": round(float(sub[_CROSS_COL].mean()), 4)
        if _CROSS_COL in sub.columns
        else 0.0,
    }


def write_drs5d_report(
    result: dict[str, Any],
    out_dir: Path | None = None,
    *,
    stamp: str | None = None,
) -> tuple[Path, Path]:
    out_dir = out_dir or (PROJECT_ROOT / "reports" / "research" / "rrg")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or date.today().strftime("%Y%m%d")
    json_path = out_dir / f"drs5d_score_backtest_{stamp}.json"
    md_path = out_dir / f"drs5d_score_backtest_{stamp}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_drs5d_markdown(result), encoding="utf-8")
    return json_path, md_path
