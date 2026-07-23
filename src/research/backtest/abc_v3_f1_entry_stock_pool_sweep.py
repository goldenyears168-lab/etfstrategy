"""ABC v3+F1 · stock pool tier filter × base gap gate × fixed TP-only.

Uses gap decomposition finding: stocks with high trailing ``gap_w5_w3_eod``
underperform at cross-section (10m Spearman ρ≈−0.31). Filters the stock
universe before event-level gap sweet-band, then evaluates champion TP-only.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from project_config import DEFAULT_ETF_CODES
from research.backtest.abc_v3_f1_entry_gap_decomposition import (
    _stock_baseline_gap_means,
)
from research.backtest.abc_v3_f1_entry_factor_scan import (
    attach_normalized_gate_features,
    build_stock_daily_gap_rv_series,
)
from research.backtest.abc_v3_f1_entry_gate_tp_sweep import collect_abc_v3_f1_raw_legs
from research.backtest.abc_v3_f1_entry_multifactor_tp_sweep import (
    BASE_GAP_BAND,
    BASE_MIN_POLL_IDX,
    CHAMPION_COMBO,
    MultifactorOverlay,
    _evaluate_cell,
    _leg_matches_overlay,
    _matches_base_gap_gate,
)
from research.backtest.finpilot_local_backtest import load_price_panels

SCHEMA = "abc_v3_f1_entry_stock_pool_sweep-v1"

PoolKey = Literal["gap_w5_w3_eod", "gap_w20_w5_eod"]
PoolMode = Literal["exclude_high", "exclude_low", "only_mid", "only_low_mid"]


@dataclass(frozen=True)
class StockPoolRule:
    id: str
    label: str
    key: PoolKey
    mode: PoolMode


def _tercile_labels(values: dict[str, float]) -> dict[str, str]:
    items = sorted(values.items(), key=lambda x: x[1])
    n = len(items)
    if n < 3:
        return {sid: "mid" for sid, _ in items}
    t1 = n // 3
    t2 = 2 * n // 3
    out: dict[str, str] = {}
    for i, (sid, _) in enumerate(items):
        if i < t1:
            out[sid] = "low"
        elif i < t2:
            out[sid] = "mid"
        else:
            out[sid] = "high"
    return out


def attach_stock_baseline_tiers(
    legs: list[dict[str, Any]],
    baseline_gap_means: dict[str, dict[str, float]],
    *,
    keys: tuple[PoolKey, ...] = ("gap_w5_w3_eod", "gap_w20_w5_eod"),
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    """Tag each leg with stock baseline gap values and low/mid/high tier."""
    tier_maps: dict[str, dict[str, str]] = {}
    for key in keys:
        vals = {
            sid: float(bg[key])
            for sid, bg in baseline_gap_means.items()
            if bg.get(key) is not None
        }
        tier_maps[key] = _tercile_labels(vals)

    out: list[dict[str, Any]] = []
    for leg in legs:
        leg = dict(leg)
        sid = str(leg.get("stock_id"))
        bg = baseline_gap_means.get(sid) or {}
        for key in keys:
            if bg.get(key) is not None:
                leg[f"stock_{key}"] = bg[key]
                leg[f"stock_tier_{key}"] = tier_maps[key].get(sid, "mid")
        out.append(leg)
    return out, tier_maps


def _leg_passes_pool_rule(leg: dict[str, Any], rule: StockPoolRule) -> bool:
    tier_key = f"stock_tier_{rule.key}"
    tier = leg.get(tier_key)
    if tier is None:
        return False
    tier = str(tier)
    if rule.mode == "exclude_high":
        return tier != "high"
    if rule.mode == "exclude_low":
        return tier != "low"
    if rule.mode == "only_mid":
        return tier == "mid"
    if rule.mode == "only_low_mid":
        return tier in ("low", "mid")
    return False


def default_stock_pool_grid() -> tuple[StockPoolRule | None, ...]:
    return (
        None,
        StockPoolRule(
            "excl_high_w5w3",
            "exclude high gap_w5_w3_eod",
            "gap_w5_w3_eod",
            "exclude_high",
        ),
        StockPoolRule(
            "only_low_mid_w5w3",
            "only low+mid gap_w5_w3_eod",
            "gap_w5_w3_eod",
            "only_low_mid",
        ),
        StockPoolRule(
            "only_mid_w5w3",
            "only mid gap_w5_w3_eod",
            "gap_w5_w3_eod",
            "only_mid",
        ),
        StockPoolRule(
            "excl_high_w20w5",
            "exclude high gap_w20_w5_eod",
            "gap_w20_w5_eod",
            "exclude_high",
        ),
        StockPoolRule(
            "only_low_mid_w20w5",
            "only low+mid gap_w20_w5_eod",
            "gap_w20_w5_eod",
            "only_low_mid",
        ),
        StockPoolRule(
            "excl_low_w20w5",
            "exclude low gap_w20_w5_eod",
            "gap_w20_w5_eod",
            "exclude_low",
        ),
    )


def _pool_none_passes(_leg: dict[str, Any]) -> bool:
    return True


def default_entry_overlay_grid() -> tuple[MultifactorOverlay, ...]:
    return (
        MultifactorOverlay("none", "entry: base only", ()),
        MultifactorOverlay("w3_improving", "entry: W3 MV>100", ("w3_improving",)),
        MultifactorOverlay("w3_rv_gte_99.5", "entry: W3 RV≥99.5", ("w3_rv_gte_99.5",)),
    )


def run_entry_stock_pool_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-09-01",
    date_end: str = "2026-07-01",
    hold_days: int = 5,
    target_pct: float = 8.0,
    min_leg_n: int = 30,
    pool_grid: tuple[StockPoolRule | None, ...] | None = None,
    entry_overlays: tuple[MultifactorOverlay, ...] | None = None,
    baseline_window: int = 60,
    min_trailing_days: int = 10,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    sample_dates = [d for d in full_dates if date_start <= d <= date_end]
    if not sample_dates:
        raise ValueError(f"no trading dates in [{date_start}, {date_end}]")

    all_legs = collect_abc_v3_f1_raw_legs(
        conn, dates=sample_dates, hold_days=hold_days, etf_codes=etf_codes
    )
    if not all_legs:
        raise ValueError("no raw ABC-V3-F1 legs in window")

    stock_ids = sorted({str(leg["stock_id"]) for leg in all_legs})
    earliest_entry = min(str(leg["entry_date"]) for leg in all_legs)
    try:
        entry_idx = full_dates.index(earliest_entry)
    except ValueError:
        entry_idx = 0
    lookback_start = max(0, entry_idx - baseline_window - 5)
    end_idx = full_dates.index(date_end) if date_end in full_dates else len(full_dates) - 1
    baseline_dates = full_dates[lookback_start : end_idx + 1]

    baseline = build_stock_daily_gap_rv_series(
        conn, stock_ids=stock_ids, dates=baseline_dates, etf_codes=etf_codes
    )
    all_legs = attach_normalized_gate_features(
        all_legs, baseline, window=baseline_window, min_trailing=min_trailing_days
    )
    baseline_gap_means = _stock_baseline_gap_means(
        baseline, before_date=date_end, window=baseline_window
    )
    all_legs, tier_maps = attach_stock_baseline_tiers(all_legs, baseline_gap_means)

    pools = pool_grid or default_stock_pool_grid()
    overlays = entry_overlays or default_entry_overlay_grid()

    cells: list[dict[str, Any]] = []
    for pool in pools:
        if pool is None:
            pool_legs = all_legs
            pool_id, pool_label = "pool_none", "no stock pool filter"
        else:
            pool_legs = [leg for leg in all_legs if _leg_passes_pool_rule(leg, pool)]
            pool_id, pool_label = pool.id, pool.label
        base_legs = [leg for leg in pool_legs if _matches_base_gap_gate(leg)]

        for overlay in overlays:
            filtered = [leg for leg in base_legs if _leg_matches_overlay(leg, overlay)]
            n = len(filtered)
            cell_id = f"{pool_id}__{overlay.id}"
            if n == 0:
                cells.append(
                    {
                        "cell_id": cell_id,
                        "pool_id": pool_id,
                        "pool_label": pool_label,
                        "overlay_id": overlay.id,
                        "overlay_label": overlay.label,
                        "n": 0,
                        "skipped": True,
                    }
                )
                continue
            row = _evaluate_cell(filtered, overlay=overlay)
            row.update(
                {
                    "cell_id": cell_id,
                    "pool_id": pool_id,
                    "pool_label": pool_label,
                    "pool_key": pool.key if pool else None,
                    "pool_mode": pool.mode if pool else None,
                    "overlay_id": overlay.id,
                    "overlay_label": overlay.label,
                    "n_pool_legs": len(pool_legs),
                    "n_base_gate": len(base_legs),
                    "skipped": False,
                    "hits_target": bool(
                        n >= min_leg_n and float(row.get("tp_only_mean_pct") or 0) >= target_pct
                    ),
                }
            )
            cells.append(row)

    cells.sort(key=lambda c: float(c.get("tp_only_mean_pct") or -999), reverse=True)
    evaluable = [c for c in cells if not c.get("skipped")]
    best = evaluable[0] if evaluable else None
    hit = [c for c in evaluable if c.get("hits_target")]

    return {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": date_end,
        "hold_days": hold_days,
        "target_pct": target_pct,
        "min_leg_n": min_leg_n,
        "n_raw_legs": len(all_legs),
        "n_stocks": len(stock_ids),
        "tier_maps_summary": {
            k: {tier: sum(1 for t in v.values() if t == tier) for tier in ("low", "mid", "high")}
            for k, v in tier_maps.items()
        },
        "base_gate": {
            "gap_band": BASE_GAP_BAND.label(),
            "min_poll_idx": BASE_MIN_POLL_IDX,
        },
        "champion_tp": CHAMPION_COMBO.tp.variant_id,
        "cells": cells,
        "best_cell": best,
        "cells_hitting_target": hit,
    }


def render_entry_stock_pool_sweep_md(payload: dict[str, Any]) -> str:
    target = float(payload.get("target_pct") or 8.0)
    min_n = int(payload.get("min_leg_n") or 30)
    best = payload.get("best_cell") or {}
    hit = payload.get("cells_hitting_target") or []

    lines = [
        "# ABC v3+F1 · stock pool tier × gap band × entry overlay × fixed TP-only",
        "",
        f"**{payload.get('n_raw_legs')}** raw legs · **{payload.get('n_stocks')}** stocks · "
        f"窗口 **{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"hold **{payload.get('hold_days')}d** · 目標 **≥{target:g}%** · n **≥{min_n}**",
        "",
        "## Stock pool tiers（trailing EOD gap）",
        "",
    ]
    for key, counts in (payload.get("tier_maps_summary") or {}).items():
        lines.append(f"- `{key}`：low={counts.get('low')} · mid={counts.get('mid')} · high={counts.get('high')}")

    lines.extend(["", f"## 目標 ≥{target:g}% · n≥{min_n}", ""])
    if hit:
        for c in hit[:8]:
            lines.append(
                f"- **可達** `{c.get('pool_label')}` + `{c.get('overlay_label')}` · "
                f"n={c.get('n')} · TP-only **{c.get('tp_only_mean_pct')}%**"
            )
    elif best:
        lines.append(
            f"**未達** — 最佳 `{best.get('pool_label')}` + `{best.get('overlay_label')}` · "
            f"n={best.get('n')} · TP-only **{best.get('tp_only_mean_pct')}%**"
        )

    lines.extend(
        [
            "",
            "## Top cells（依 TP-only 均報酬）",
            "",
            "| Pool | Entry overlay | n | hold% | TP-only% | Δ | ≥8% | 達標 |",
            "|------|---------------|--:|------:|---------:|--:|----:|:----:|",
        ]
    )
    for c in (payload.get("cells") or [])[:15]:
        if c.get("skipped"):
            continue
        flag = "✓" if c.get("hits_target") else "—"
        lines.append(
            f"| {c.get('pool_label')} | {c.get('overlay_label')} | {c.get('n')} | "
            f"{c.get('hold_only_mean_pct')} | {c.get('tp_only_mean_pct')} | "
            f"{c.get('delta_vs_hold_pp')} | {c.get('pct_ge_8')} | {flag} |"
        )
    lines.extend(
        [
            "",
            "## 設計說明",
            "",
            "- Stock pool：依 gap 分解，排除 trailing `gap_w5_w3_eod` 最高 tertile（截面偏弱股）",
            "- Event gate：gap band `[2,8]×[-1,2]` · poll≥1",
            "- Exit：champion TP-only · NEVER_SL",
        ]
    )
    return "\n".join(lines)
