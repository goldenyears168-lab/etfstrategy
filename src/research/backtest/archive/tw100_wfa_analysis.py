"""TW100 walk-forward post-analysis · IC tear sheet + Breadth zone stratification (G3)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from market_breadth_ma import (
    BREADTH_ZONE_DISPLAY,
    BREADTH_ZONES_ORDER,
    breadth_map_by_date,
    build_breadth_panel,
)
from research.backtest.archive.tw100_walk_forward import _ic_stats


@dataclass(frozen=True)
class Tw100WfaAnalysisConfig:
    wfa_json: Path
    snapshot_date: str = "2026-06-26"
    breadth_axis: str = "200"  # zone_200 per config/regime.yaml breadth diagnostic
    min_zone_days: int = 20
    g3_fail_ic_threshold: float = -0.02


def load_wfa_payload(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"missing WFA artifact: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _fold_decay_rows(folds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fold in folds:
        valid = fold.get("valid_ic") or {}
        test = fold.get("test_ic") or {}
        v_mean = valid.get("mean")
        t_mean = test.get("mean")
        decay = None
        if v_mean is not None and t_mean is not None and v_mean != 0:
            decay = round(float(t_mean) / float(v_mean), 4)
        rows.append(
            {
                "fold_id": fold.get("fold_id"),
                "test_start": fold.get("test_start"),
                "test_end": fold.get("test_end"),
                "best_iteration": fold.get("best_iteration"),
                "valid_ic_mean": v_mean,
                "valid_icir": valid.get("icir"),
                "test_ic_mean": t_mean,
                "test_icir": test.get("icir"),
                "valid_to_test_decay": decay,
            }
        )
    return rows


def _collect_oos_daily_ic(folds: list[dict[str, Any]]) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for fold in folds:
        daily = fold.get("test_ic_daily") or []
        for row in daily:
            if isinstance(row, dict):
                d, ic = row.get("date"), row.get("ic")
            elif isinstance(row, (list, tuple)) and len(row) == 2:
                d, ic = row[0], row[1]
            else:
                continue
            if d is not None and ic is not None:
                out.append((str(d), float(ic)))
    return out


def build_ic_tear_sheet(payload: dict[str, Any]) -> dict[str, Any]:
    folds = payload.get("folds") or []
    fold_rows = _fold_decay_rows(folds)
    valid_means = [r["valid_ic_mean"] for r in fold_rows if r["valid_ic_mean"] is not None]
    test_means = [r["test_ic_mean"] for r in fold_rows if r["test_ic_mean"] is not None]
    decays = [r["valid_to_test_decay"] for r in fold_rows if r["valid_to_test_decay"] is not None]
    best_iters = [int(f.get("best_iteration") or 0) for f in folds]

    agg_valid = _ic_stats(valid_means) if valid_means else {"n_days": 0, "mean": None, "icir": None}
    agg_test_fold = _ic_stats(test_means) if test_means else {"n_days": 0, "mean": None, "icir": None}

    return {
        "schema": "tw100_ic_tear_sheet-v1",
        "source_schema": payload.get("schema"),
        "model": payload.get("model") or payload.get("signal"),
        "features": payload.get("features"),
        "n_folds": len(folds),
        "aggregate_test_ic": payload.get("aggregate_test_ic"),
        "fold_mean_valid_ic": agg_valid,
        "fold_mean_test_ic": agg_test_fold,
        "valid_to_test_decay_mean": round(sum(decays) / len(decays), 4) if decays else None,
        "best_iteration_mean": round(sum(best_iters) / len(best_iters), 2) if best_iters else None,
        "folds": fold_rows,
    }


def build_regime_stratification(
    payload: dict[str, Any],
    conn: sqlite3.Connection,
    cfg: Tw100WfaAnalysisConfig,
) -> dict[str, Any]:
    folds = payload.get("folds") or []
    daily_ic = _collect_oos_daily_ic(folds)
    if not daily_ic:
        return {
            "schema": "tw100_regime_stratification-v1",
            "breadth_axis": f"zone_{cfg.breadth_axis}",
            "status": "skipped",
            "reason": "WFA artifact lacks test_ic_daily; re-run run_tw100_alpha158_lgbm_wfa.py",
        }

    dates = [d for d, _ in daily_ic]
    panel = build_breadth_panel(
        conn,
        date_start=min(dates),
        date_end=max(dates),
    )
    zone_by_date = breadth_map_by_date(panel, use=cfg.breadth_axis)  # type: ignore[arg-type]

    by_zone: dict[str, list[float]] = {z: [] for z in BREADTH_ZONES_ORDER}
    unknown = 0
    for d, ic in daily_ic:
        zone = zone_by_date.get(d, "unknown")
        if zone in by_zone:
            by_zone[zone].append(ic)
        else:
            unknown += 1

    zone_rows: list[dict[str, Any]] = []
    failing_zones: list[str] = []
    for zone in BREADTH_ZONES_ORDER:
        vals = by_zone[zone]
        stats = _ic_stats(vals)
        zone_rows.append(
            {
                "breadth_zone": zone,
                "display": BREADTH_ZONE_DISPLAY.get(zone, zone),
                **stats,
            }
        )
        mean = stats.get("mean")
        n = int(stats.get("n_days") or 0)
        if n >= cfg.min_zone_days and mean is not None and float(mean) < cfg.g3_fail_ic_threshold:
            failing_zones.append(zone)

    all_ic = [ic for _, ic in daily_ic]
    g3_passed = not failing_zones
    return {
        "schema": "tw100_regime_stratification-v1",
        "breadth_axis": f"zone_{cfg.breadth_axis}",
        "status": "ok",
        "n_oos_days": len(daily_ic),
        "n_unknown_zone_days": unknown,
        "min_zone_days": cfg.min_zone_days,
        "g3_fail_ic_threshold": cfg.g3_fail_ic_threshold,
        "g3_regime_stratification": "passed" if g3_passed else "failed",
        "failing_zones": failing_zones,
        "aggregate_oos_ic": _ic_stats(all_ic),
        "by_breadth_zone": zone_rows,
    }


def build_wfa_analysis_report(
    payload: dict[str, Any],
    conn: sqlite3.Connection | None,
    cfg: Tw100WfaAnalysisConfig,
) -> dict[str, Any]:
    tear = build_ic_tear_sheet(payload)
    regime: dict[str, Any]
    if conn is None:
        regime = {
            "schema": "tw100_regime_stratification-v1",
            "status": "skipped",
            "reason": "no DB connection",
        }
    else:
        regime = build_regime_stratification(payload, conn, cfg)

    return {
        "schema": "tw100_wfa_analysis-v1",
        "layer": "research",
        "source_wfa": str(cfg.wfa_json),
        "ic_tear_sheet": tear,
        "regime_stratification": regime,
    }


def render_wfa_analysis_markdown(report: dict[str, Any]) -> str:
    tear = report.get("ic_tear_sheet") or {}
    regime = report.get("regime_stratification") or {}
    agg = tear.get("aggregate_test_ic") or {}
    lines = [
        "# TW100 WFA Analysis · IC tear sheet + Regime stratification",
        "",
        f"- Source: `{report.get('source_wfa', '')}`",
        f"- Model: {tear.get('model') or '—'} · Features: {tear.get('features') or '—'}",
        "",
        "## Aggregate OOS Rank IC",
        "",
        f"| metric | value |",
        f"|--------|-------|",
        f"| test IC mean | {agg.get('mean')} |",
        f"| test ICIR | {agg.get('icir')} |",
        f"| OOS days | {agg.get('n_days')} |",
        f"| fold-mean valid IC | {(tear.get('fold_mean_valid_ic') or {}).get('mean')} |",
        f"| fold-mean test IC | {(tear.get('fold_mean_test_ic') or {}).get('mean')} |",
        f"| valid→test decay (mean) | {tear.get('valid_to_test_decay_mean')} |",
        f"| best_iteration (mean) | {tear.get('best_iteration_mean')} |",
        "",
        "## Per-fold IC decay (valid → test)",
        "",
        "| fold | test window | best_iter | valid IC | test IC | decay |",
        "|------|-------------|-----------|----------|---------|-------|",
    ]
    for row in tear.get("folds") or []:
        lines.append(
            f"| {row.get('fold_id')} | {row.get('test_start')}..{row.get('test_end')} "
            f"| {row.get('best_iteration')} | {row.get('valid_ic_mean')} "
            f"| {row.get('test_ic_mean')} | {row.get('valid_to_test_decay')} |"
        )

    lines.extend(["", "## G3 · Breadth zone stratification", ""])
    if regime.get("status") != "ok":
        lines.append(f"_Skipped: {regime.get('reason', regime.get('status'))}_")
    else:
        g3 = regime.get("g3_regime_stratification", "pending")
        lines.append(f"**G3 status:** {g3}")
        if regime.get("failing_zones"):
            lines.append(f"- Failing zones (IC < {regime.get('g3_fail_ic_threshold')}): "
                         + ", ".join(regime["failing_zones"]))
        lines.extend(
            [
                "",
                "| Breadth zone | n_days | IC mean | ICIR |",
                "|--------------|--------|---------|------|",
            ]
        )
        for row in regime.get("by_breadth_zone") or []:
            lines.append(
                f"| {row.get('display', row.get('breadth_zone'))} "
                f"| {row.get('n_days')} | {row.get('mean')} | {row.get('icir')} |"
            )

    lines.append("")
    return "\n".join(lines)


def write_wfa_analysis_artifact(report: dict[str, Any], out_json: Path, out_md: Path | None = None) -> Path:
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md is not None:
        out_md.write_text(render_wfa_analysis_markdown(report), encoding="utf-8")
    return out_json
