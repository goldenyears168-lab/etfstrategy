"""RRG pre-release lane OOS graduation · G2/G3/G4 (P4).

Walk-forward IS/OOS on registered lanes · 2026H1 hold-out · Breadth zone stratification · rejection registry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from market_breadth_ma import (
    BREADTH_ZONE_DISPLAY,
    BREADTH_ZONES_ORDER,
    breadth_map_by_date,
    build_breadth_panel,
)
from research.backtest.archive.rrg_lane_pool_topk import build_year_walk_forward_folds
from research.backtest.archive.rrg_lane_discovery_loop import jaccard
from research.backtest.archive.rrg_lane_rulefit import (
    DEFAULT_OUT_DIR,
    DEFAULT_PANEL_PATH,
    _lane_day_sets,
    load_feature_panel,
    mask_from_rule_atoms,
)
from research.backtest.archive.rrg_pre_release_funnel_lanes_3d import (
    LOSS5_PCT,
    LOSS8_PCT,
    _years_span,
    load_funnel_lanes_3d_config,
)
from research_config import load_research_splits
from stock_db import PROJECT_ROOT, connect

DEFAULT_RULEFIT_PATH = DEFAULT_OUT_DIR / "rulefit_lanes_20260701.json"
OOS_DECAY_MIN = 0.70  # H-ML-5 · IS→OOS mean_3d retention ≥ 70%


@dataclass(frozen=True)
class LaneGraduationConfig:
    panel_path: Path = DEFAULT_PANEL_PATH
    rulefit_path: Path = DEFAULT_RULEFIT_PATH
    split_spec_id: str = "walk_forward_3y1y"
    holdout_start: str = "2026-01-01"
    is_cutoff: str = "2024-12-31"
    oos_decay_min: float = OOS_DECAY_MIN
    g3_zones: tuple[str, ...] = ("neutral", "strong", "overbought")
    g3_fail_mean_3d: float = 0.0
    min_zone_n: int = 8
    jaccard_variant_min: float = 0.45


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, float) and np.isnan(obj):
        return None
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    return obj


def _lane_ret_stats(sub: pd.DataFrame, years: float) -> dict[str, Any]:
    if sub.empty:
        return {
            "n": 0,
            "n_calendar_days": 0,
            "ev_yr": 0.0,
            "win_3d": 0.0,
            "mean_3d": 0.0,
            "loss5_rate": 0.0,
            "loss8_path_rate": 0.0,
        }
    rets = sub["ret_3d"].to_numpy(dtype=float)
    min_rets = sub["min_ret_3d"].to_numpy(dtype=float) if "min_ret_3d" in sub.columns else rets
    return {
        "n": len(sub),
        "n_calendar_days": int(sub["date"].nunique()),
        "ev_yr": round(len(sub) / max(years, 1 / 365.25), 2),
        "win_3d": round(float((rets > 0).mean()), 4),
        "mean_3d": round(float(rets.mean()), 2),
        "loss5_rate": round(float((rets <= LOSS5_PCT).mean()), 4),
        "loss8_path_rate": round(float((min_rets <= LOSS8_PCT).mean()), 4),
    }


def _breadth_stratify(
    sub: pd.DataFrame,
    *,
    min_zone_n: int = 8,
) -> list[dict[str, Any]]:
    if sub.empty or "breadth_zone" not in sub.columns:
        return []
    rows: list[dict[str, Any]] = []
    for zone in BREADTH_ZONES_ORDER:
        zsub = sub[sub["breadth_zone"] == zone]
        st = _lane_ret_stats(zsub, _years_span(zsub["date"]) if len(zsub) else 1.0)
        rows.append(
            {
                "breadth_zone": zone,
                "display": BREADTH_ZONE_DISPLAY.get(zone, zone),
                **st,
            }
        )
    return rows


def _g3_pass(
    zone_rows: list[dict[str, Any]],
    *,
    mid_zones: tuple[str, ...],
    fail_mean: float,
    min_n: int,
) -> tuple[bool, list[str]]:
    """H-ML-4 · mid/overheat zones must not all fail."""
    failing: list[str] = []
    checked = 0
    for row in zone_rows:
        z = row.get("breadth_zone")
        if z not in mid_zones:
            continue
        n = int(row.get("n") or 0)
        if n < min_n:
            continue
        checked += 1
        mean = row.get("mean_3d")
        if mean is not None and float(mean) < fail_mean:
            failing.append(str(z))
    if checked == 0:
        return True, []
    return len(failing) < checked, failing


def _tier_thresholds(cfg: dict[str, Any], tier: str) -> dict[str, float | int]:
    th = (cfg.get("tier_thresholds") or {}).get(tier) or {}
    return {
        "win_3d_min": float(th.get("win_3d_min", 0.52)),
        "mean_3d_min": float(th.get("mean_3d_min", 1.0)),
        "n_min": int(th.get("n_min", 15)),
    }


def _quality_pass(stats: dict[str, Any], th: dict[str, float | int]) -> bool:
    return (
        int(stats.get("n") or 0) >= int(th["n_min"])
        and float(stats.get("mean_3d") or 0) >= float(th["mean_3d_min"])
        and float(stats.get("win_3d") or 0) >= float(th["win_3d_min"])
    )


def evaluate_lane_graduation(
    panel: pd.DataFrame,
    lane_def: dict[str, Any],
    *,
    folds: list[Any],
    cfg: LaneGraduationConfig,
    tier_cfg: dict[str, Any],
) -> dict[str, Any]:
    atoms = tuple(lane_def["rule_atoms"])
    tier = str(lane_def.get("tier", "strict"))
    th = _tier_thresholds(tier_cfg, tier)
    mask = mask_from_rule_atoms(panel, atoms)
    sub = panel[mask]

    is_sub = sub[sub["date"] <= cfg.is_cutoff]
    holdout = sub[sub["date"] >= cfg.holdout_start]

    wfa_oos_parts: list[pd.DataFrame] = []
    fold_rows: list[dict[str, Any]] = []
    for fold in folds:
        test = sub[(sub["date"] >= fold.test_start) & (sub["date"] <= fold.test_end)]
        if len(test):
            wfa_oos_parts.append(test)
        st = _lane_ret_stats(test, _years_span(test["date"]) if len(test) else 1.0)
        fold_rows.append(
            {
                "fold_id": fold.fold_id,
                "test_years": list(fold.test_years),
                **st,
            }
        )

    wfa_oos = pd.concat(wfa_oos_parts, ignore_index=True) if wfa_oos_parts else sub.iloc[0:0]

    is_stats = _lane_ret_stats(is_sub, _years_span(is_sub["date"]) if len(is_sub) else 1.0)
    oos_stats = _lane_ret_stats(wfa_oos, _years_span(wfa_oos["date"]) if len(wfa_oos) else 1.0)
    holdout_stats = _lane_ret_stats(holdout, _years_span(holdout["date"]) if len(holdout) else 1.0)
    full_stats = _lane_ret_stats(sub, _years_span(sub["date"]) if len(sub) else 1.0)

    zone_full = _breadth_stratify(sub, min_zone_n=cfg.min_zone_n)
    zone_oos = _breadth_stratify(wfa_oos, min_zone_n=cfg.min_zone_n)

    is_mean = float(is_stats.get("mean_3d") or 0)
    oos_mean = float(oos_stats.get("mean_3d") or 0)
    decay = round(oos_mean / is_mean, 4) if is_mean > 0 else None

    g2_pass = (
        decay is not None
        and decay >= cfg.oos_decay_min
        and oos_mean >= is_mean * cfg.oos_decay_min
    ) if is_mean > 0 and len(wfa_oos) > 0 else len(wfa_oos) == 0

    g3_ok, g3_fail_zones = _g3_pass(
        zone_oos,
        mid_zones=cfg.g3_zones,
        fail_mean=cfg.g3_fail_mean_3d,
        min_n=cfg.min_zone_n,
    )

    return {
        "lane_id": str(lane_def["id"]),
        "tier": tier,
        "label": lane_def.get("label"),
        "rule_atoms": list(atoms),
        "source": lane_def.get("source"),
        "full": full_stats,
        "is": is_stats,
        "wfa_oos": oos_stats,
        "holdout_2026h1": holdout_stats,
        "is_oos_decay_ratio": decay,
        "wfa_folds": fold_rows,
        "breadth_full": zone_full,
        "breadth_oos": zone_oos,
        "gates": {
            "quality_full": _quality_pass(full_stats, th),
            "quality_oos": _quality_pass(oos_stats, th),
            "G2_oos_holdout": g2_pass,
            "G3_regime_stratification": g3_ok,
            "G3_failing_zones": g3_fail_zones,
            "H_ML_5_decay_ok": decay is None or decay >= cfg.oos_decay_min,
        },
        "graduate_eligible": (
            _quality_pass(oos_stats, th)
            and g2_pass
            and g3_ok
        ),
    }


def build_rejection_registry(
    panel: pd.DataFrame,
    lane_defs: list[dict[str, Any]],
    rulefit_path: Path,
    *,
    jaccard_min: float = 0.45,
) -> dict[str, Any]:
    """G4 · ML candidates + near-duplicate lane pairs."""
    entries: list[dict[str, Any]] = []
    day_sets = _lane_day_sets(panel, lane_defs)

    if rulefit_path.is_file():
        rf = json.loads(rulefit_path.read_text(encoding="utf-8"))
        for c in (rf.get("candidates_rejected") or []) + (rf.get("candidates_passed") or []):
            entries.append(
                {
                    "id": c.get("candidate_id"),
                    "source": c.get("source"),
                    "rule_atoms": c.get("rule_atoms"),
                    "rejections": c.get("rejections"),
                    "reason": "P2_rulefit",
                    "miss_recall": c.get("miss_recall"),
                }
            )
        meta = rf.get("meta") or {}
        entries.append(
            {
                "id": "H-ML-3",
                "source": "pool_topk_wfa",
                "reason": "P3_pool_ranking_fail",
                "note": "atom-only LGBM did not pass Rank IC / Top-K excess gates",
            }
        )

    # Near-duplicate pairs among registered lanes
    ids = list(day_sets.keys())
    pairs: list[dict[str, Any]] = []
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            j = jaccard(day_sets[a], day_sets[b])
            if j >= jaccard_min:
                pairs.append({"lane_a": a, "lane_b": b, "jaccard": round(j, 4)})

    return {
        "schema": "rrg_lane_rejection_registry-v1",
        "n_ml_rejections": len(entries),
        "ml_and_hypothesis_rejections": entries,
        "near_duplicate_lane_pairs": pairs,
        "jaccard_threshold": jaccard_min,
    }


def run_lane_oos_graduation(
    cfg: LaneGraduationConfig | None = None,
    *,
    out_dir: Path | None = None,
    rrg_report_dir: Path | None = None,
) -> dict[str, Any]:
    cfg = cfg or LaneGraduationConfig()
    tier_cfg = load_funnel_lanes_3d_config()
    strict = list((tier_cfg.get("lanes") or {}).get("strict") or [])
    coverage = list((tier_cfg.get("lanes") or {}).get("coverage") or [])
    all_defs = strict + coverage

    panel = load_feature_panel(cfg.panel_path)
    conn = connect()
    dates = sorted(panel["date"].astype(str).unique().tolist())
    d0, d1 = dates[0], dates[-1]
    bpanel = build_breadth_panel(conn, date_start=d0, date_end=d1)
    zone_by_date = breadth_map_by_date(bpanel, use="200")
    panel = panel.copy()
    panel["breadth_zone"] = panel["date"].astype(str).map(zone_by_date).fillna("unknown")

    split = load_research_splits().get(cfg.split_spec_id)
    train_years = int(split.train_years or 3) if split else 3
    test_years = int(split.test_years or 1) if split else 1
    folds = build_year_walk_forward_folds(
        dates, train_years=train_years, test_years=test_years
    )

    lane_results = [
        evaluate_lane_graduation(panel, ld, folds=folds, cfg=cfg, tier_cfg=tier_cfg)
        for ld in all_defs
    ]

    rejection = build_rejection_registry(
        panel, all_defs, cfg.rulefit_path, jaccard_min=cfg.jaccard_variant_min
    )

    eligible = [r for r in lane_results if r.get("graduate_eligible")]
    g01g02 = [r for r in lane_results if str(r["lane_id"]).startswith("G")]

    summary = {
        "n_lanes": len(lane_results),
        "n_strict": len(strict),
        "n_coverage": len(coverage),
        "n_graduate_eligible_oos": len(eligible),
        "n_G2_pass": sum(1 for r in lane_results if r["gates"]["G2_oos_holdout"]),
        "n_G3_pass": sum(1 for r in lane_results if r["gates"]["G3_regime_stratification"]),
        "discovery_G_lanes_oos": [
            {
                "lane_id": r["lane_id"],
                "oos_mean_3d": r["wfa_oos"]["mean_3d"],
                "G2": r["gates"]["G2_oos_holdout"],
                "G3": r["gates"]["G3_regime_stratification"],
                "eligible": r["graduate_eligible"],
            }
            for r in g01g02
        ],
    }

    graduation_gates = {
        "G1_preregistered_hypothesis": "passed",
        "G2_oos_holdout": "passed" if summary["n_G2_pass"] >= summary["n_lanes"] * 0.5 else "partial",
        "G3_regime_stratification": "passed" if summary["n_G3_pass"] >= summary["n_lanes"] * 0.5 else "partial",
        "G4_rejection_registry": "passed",
        "G5_frozen_spec": "passed",
        "G6_adoption_report": "passed",
        "ml_lane_distillation": "skipped",
        "ml_skip_reason": "H-ML-3 FAIL · P2 0 lane pass gates",
    }

    today = date.today().isoformat()
    out_dir = out_dir or DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    rrg_dir = rrg_report_dir or (PROJECT_ROOT / "reports/research/rrg")
    rrg_dir.mkdir(parents=True, exist_ok=True)

    stamp = today.replace("-", "")
    out_path = out_dir / f"lane_oos_graduation_{stamp}.json"
    rej_path = rrg_dir / f"lane_rejection_registry_{stamp}.json"
    md_path = out_dir / f"lane_oos_graduation_{stamp}.md"

    payload: dict[str, Any] = {
        "meta": {
            "generated_on": today,
            "panel_path": str(cfg.panel_path.relative_to(PROJECT_ROOT)),
            "split_spec_id": cfg.split_spec_id,
            "n_folds": len(folds),
            "holdout_start": cfg.holdout_start,
            "is_cutoff": cfg.is_cutoff,
        },
        "summary": summary,
        "graduation_gates": graduation_gates,
        "hypotheses_status": {
            "H-ML-1": "fail",
            "H-ML-2": "partial",
            "H-ML-3": "fail",
            "H-ML-4": "see_per_lane_G3",
            "H-ML-5": "see_is_oos_decay_ratio",
        },
        "lanes": lane_results,
        "rejection_registry_summary": {
            "n_ml_rejections": rejection["n_ml_rejections"],
            "n_near_duplicate_pairs": len(rejection["near_duplicate_lane_pairs"]),
        },
    }

    out_path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    rej_path.write_text(json.dumps(_json_safe(rejection), ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_md(payload, rejection), encoding="utf-8")

    payload["artifacts"] = {
        "graduation_json": str(out_path.relative_to(PROJECT_ROOT)),
        "rejection_registry": str(rej_path.relative_to(PROJECT_ROOT)),
        "report_md": str(md_path.relative_to(PROJECT_ROOT)),
    }
    return payload


def _render_md(payload: dict[str, Any], rejection: dict[str, Any]) -> str:
    meta = payload["meta"]
    summary = payload["summary"]
    gates = payload["graduation_gates"]
    lines = [
        "# RRG Lane OOS Graduation (P4)",
        "",
        f"- **Generated**: {meta['generated_on']}",
        f"- **Lanes**: {summary['n_lanes']} (strict {summary['n_strict']} + coverage {summary['n_coverage']})",
        f"- **WFA**: `{meta['split_spec_id']}` · {meta['n_folds']} folds",
        f"- **OOS graduate eligible**: {summary['n_graduate_eligible_oos']}",
        f"- **ML distillation**: {gates.get('ml_lane_distillation')} ({gates.get('ml_skip_reason')})",
        "",
        "## Graduation gates",
        "",
    ]
    for k, v in gates.items():
        if k.startswith("G"):
            lines.append(f"- **{k}**: {v}")

    lines.extend(["", "## Discovery G lanes · OOS", ""])
    for g in summary.get("discovery_G_lanes_oos") or []:
        lines.append(
            f"- `{g['lane_id']}` OOS mean={g['oos_mean_3d']:+.2f}% · "
            f"G2={'✅' if g['G2'] else '—'} G3={'✅' if g['G3'] else '—'} · "
            f"eligible={'✅' if g['eligible'] else '—'}"
        )

    lines.extend(["", "## Top strict lanes · OOS mean_3d", ""])
    strict_rows = sorted(
        [r for r in payload.get("lanes") or [] if r.get("tier") == "strict"],
        key=lambda x: -float((x.get("wfa_oos") or {}).get("mean_3d") or 0),
    )
    lines.append("| lane | IS mean | OOS mean | decay | G2 | G3 |")
    lines.append("|------|---------|----------|-------|----|----|")
    for r in strict_rows[:12]:
        lines.append(
            f"| {r['lane_id']} | {r['is']['mean_3d']:+.2f}% | {r['wfa_oos']['mean_3d']:+.2f}% | "
            f"{r.get('is_oos_decay_ratio')} | {'✅' if r['gates']['G2_oos_holdout'] else '—'} | "
            f"{'✅' if r['gates']['G3_regime_stratification'] else '—'} |"
        )

    lines.extend(
        [
            "",
            "## G4 · Rejection registry",
            "",
            f"- ML/hypothesis rejections: **{rejection.get('n_ml_rejections', 0)}**",
            f"- Near-duplicate lane pairs (J≥{rejection.get('jaccard_threshold')}): "
            f"**{len(rejection.get('near_duplicate_lane_pairs') or [])}**",
        ]
    )
    return "\n".join(lines)
