"""ABC v3+F1 · gap-band base gate × multi-factor overlay × fixed TP-only · hold 5d.

Stacks lightweight entry filters from w3_rv_hl_winner_profile on the 3-month
gap-band winner (gap20-5[2,8]·gap5-3[-1,2]·poll≥1, no rebound) and evaluates
each cell with the champion TP-only exit from abc_v3_f1_kinematic_tp_sl_study.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from project_config import DEFAULT_ETF_CODES
from research.backtest.abc_v3_f1_entry_gate_tp_sweep import collect_abc_v3_f1_raw_legs
from research.backtest.abc_v3_f1_entry_structure_sweep import GapSweetBand
from research.backtest.archive.abc_v3_f1_kinematic_tp_sl_study import (
    NEVER_SL,
    TakeProfitParams,
    TpSlCombo,
    _summarize_tp_sl,
    evaluate_leg_tp_sl,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.w3_rv_hl_winner_profile import _trade_matches_filter

BASE_GAP_BAND = GapSweetBand(
    gap_w20_w5_min=2.0,
    gap_w20_w5_max=8.0,
    gap_w5_w3_min=-1.0,
    gap_w5_w3_max=2.0,
)
BASE_MIN_POLL_IDX = 1

CHAMPION_TP = TakeProfitParams(
    gw5_min=4.0,
    gw3_min=1.0,
    rv_leg="w3",
    rv_mode="poll_drop",
    drv_min=0.25,
    min_ret_pct=5.0,
)
CHAMPION_COMBO = TpSlCombo(tp=CHAMPION_TP, sl=NEVER_SL)


@dataclass(frozen=True)
class MultifactorOverlay:
    id: str
    label: str
    filter_ids: tuple[str, ...]


def _leg_entry_trade_dict(leg: dict[str, Any]) -> dict[str, Any]:
    """Map leg entry_t0 kinematic snap to _trade_matches_filter trade shape."""
    t0 = leg.get("entry_t0") or {}
    rv3 = t0.get("w3_rv")
    rv20 = t0.get("w20_rv")
    out: dict[str, Any] = {
        "w3_rv": rv3,
        "w3_mv": t0.get("w3_mv"),
        "w5_mv": t0.get("w5_mv"),
        "w20_rv": rv20,
        "w3_quadrant": t0.get("w3_quad"),
        "spread_class": t0.get("spread_class"),
    }
    if rv3 is not None:
        out["w3_rv_shallow"] = float(rv3) >= 99.5
    if rv20 is not None and rv3 is not None:
        out["w20_w3_rv_spread"] = round(float(rv20) - float(rv3), 3)
    return out


def _matches_base_gap_gate(leg: dict[str, Any]) -> bool:
    poll_idx = leg.get("gate_poll_idx")
    if poll_idx is None or int(poll_idx) < BASE_MIN_POLL_IDX:
        return False
    gap20_5 = leg.get("gate_gap_w20_w5")
    gap5_3 = leg.get("gate_gap_w5_w3")
    if gap20_5 is None or gap5_3 is None:
        return False
    band = BASE_GAP_BAND
    return (
        band.gap_w20_w5_min <= float(gap20_5) <= band.gap_w20_w5_max
        and band.gap_w5_w3_min <= float(gap5_3) <= band.gap_w5_w3_max
    )


def _leg_matches_overlay(leg: dict[str, Any], overlay: MultifactorOverlay) -> bool:
    if not overlay.filter_ids:
        return True
    trade = _leg_entry_trade_dict(leg)
    return all(_trade_matches_filter(trade, fid) for fid in overlay.filter_ids)


def default_multifactor_overlay_grid() -> tuple[MultifactorOverlay, ...]:
    singles = (
        ("none", "base only", ()),
        ("w3_improving", "W3 MV>100", ("w3_improving",)),
        ("avoid_w3_lagging", "W3 ∉ Lagging", ("avoid_w3_lagging",)),
        ("w20_rv_lte_105.5", "W20 RV≤105.5", ("w20_rv_lte_105.5",)),
        ("w5_mv_gt_100", "W5 MV>100", ("w5_mv_gt_100",)),
        ("spread_pullback", "spread pullback", ("spread_pullback",)),
        ("w3_rv_gte_99.5", "W3 RV≥99.5", ("w3_rv_gte_99.5",)),
        ("w20_w3_spread_lte_6", "W20-W3 RV spread≤6", ("w20_w3_spread_lte_6",)),
    )
    combos = (
        (
            "w3_improving+avoid_lag",
            "W3 improving · avoid lagging",
            ("w3_improving", "avoid_w3_lagging"),
        ),
        (
            "w3_improving+w20_rv105.5",
            "W3 improving · W20 RV≤105.5",
            ("w3_improving", "w20_rv_lte_105.5"),
        ),
        (
            "w3_improving+w5_mv",
            "W3 improving · W5 MV>100",
            ("w3_improving", "w5_mv_gt_100"),
        ),
        (
            "avoid_lag+w20_rv105.5",
            "avoid lagging · W20 RV≤105.5",
            ("avoid_w3_lagging", "w20_rv_lte_105.5"),
        ),
        (
            "w3_improving+avoid_lag+w20_rv105.5",
            "W3 improving · avoid lag · W20 RV≤105.5",
            ("w3_improving", "avoid_w3_lagging", "w20_rv_lte_105.5"),
        ),
        (
            "w3_improving+avoid_lag+spread_pb",
            "W3 improving · avoid lag · spread pullback",
            ("w3_improving", "avoid_w3_lagging", "spread_pullback"),
        ),
        (
            "w3_improving+w20_rv105.5+spread_lte6",
            "W3 improving · W20 RV≤105.5 · spread≤6",
            ("w3_improving", "w20_rv_lte_105.5", "w20_w3_spread_lte_6"),
        ),
    )
    cells: list[MultifactorOverlay] = []
    for oid, label, fids in (*singles, *combos):
        cells.append(MultifactorOverlay(id=oid, label=label, filter_ids=fids))
    return tuple(cells)


def _evaluate_cell(
    legs: list[dict[str, Any]],
    *,
    overlay: MultifactorOverlay,
    combo: TpSlCombo = CHAMPION_COMBO,
) -> dict[str, Any]:
    hold_only = _summarize_tp_sl(
        legs,
        [{"fired": False, "exit_ret_pct": float(l["return_pct"])} for l in legs],
        variant_id="hold_only",
    )
    tp_results = [evaluate_leg_tp_sl(leg, combo) for leg in legs]
    tp_only = _summarize_tp_sl(
        legs,
        tp_results,
        variant_id=combo.tp.variant_id,
        combo=combo,
    )
    hold_mean = float(hold_only.get("mean_all_legs_ret_pct") or 0)
    tp_mean = float(tp_only.get("mean_all_legs_ret_pct") or 0)
    return {
        "overlay_id": overlay.id,
        "overlay_label": overlay.label,
        "filter_ids": list(overlay.filter_ids),
        "n": len(legs),
        "hold_only_mean_pct": hold_mean,
        "tp_only_mean_pct": tp_mean,
        "delta_vs_hold_pp": round(tp_mean - hold_mean, 3),
        "tp_fire_rate_pct": tp_only.get("fire_rate_pct"),
        "pct_ge_8": tp_only.get("pct_legs_hit_8"),
        "pct_ge_10": tp_only.get("pct_legs_hit_10"),
        "tp_variant_id": combo.tp.variant_id,
    }


def run_entry_multifactor_tp_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2026-04-01",
    date_end: str = "2026-07-07",
    hold_days: int = 5,
    target_pct: float = 8.0,
    min_leg_n: int = 30,
    overlay_grid: tuple[MultifactorOverlay, ...] | None = None,
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

    base_legs = [leg for leg in all_legs if _matches_base_gap_gate(leg)]
    grid = overlay_grid or default_multifactor_overlay_grid()

    cells: list[dict[str, Any]] = []
    for overlay in grid:
        filtered = [leg for leg in base_legs if _leg_matches_overlay(leg, overlay)]
        n = len(filtered)
        if n == 0:
            cells.append(
                {
                    "overlay_id": overlay.id,
                    "overlay_label": overlay.label,
                    "filter_ids": list(overlay.filter_ids),
                    "n": 0,
                    "skipped": True,
                }
            )
            continue
        row = _evaluate_cell(filtered, overlay=overlay)
        row["skipped"] = False
        row["hits_target"] = bool(
            n >= min_leg_n and float(row.get("tp_only_mean_pct") or 0) >= target_pct
        )
        cells.append(row)

    cells.sort(key=lambda c: float(c.get("tp_only_mean_pct") or -999), reverse=True)
    evaluable = [c for c in cells if not c.get("skipped")]
    best = evaluable[0] if evaluable else None
    hit_cells = [c for c in evaluable if c.get("hits_target")]
    base_only = next((c for c in cells if c.get("overlay_id") == "none"), None)

    return {
        "schema": "abc_v3_f1_entry_multifactor_tp_sweep-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": date_end,
        "hold_days": hold_days,
        "target_pct": target_pct,
        "min_leg_n": min_leg_n,
        "n_raw_legs": len(all_legs),
        "n_base_gate_legs": len(base_legs),
        "base_gate": {
            "gap_band": BASE_GAP_BAND.label(),
            "min_poll_idx": BASE_MIN_POLL_IDX,
            "rebound": "none",
        },
        "champion_tp": CHAMPION_TP.variant_id,
        "base_gate_cell": base_only,
        "cells": cells,
        "best_cell": best,
        "cells_hitting_target": hit_cells,
    }


def render_entry_multifactor_tp_sweep_md(payload: dict[str, Any]) -> str:
    target = float(payload.get("target_pct") or 8.0)
    min_n = int(payload.get("min_leg_n") or 30)
    base = payload.get("base_gate_cell") or {}
    best = payload.get("best_cell") or {}
    hit = payload.get("cells_hitting_target") or []
    bg = payload.get("base_gate") or {}

    lines = [
        f"# ABC v3+F1 · gap band × multi-factor overlay × fixed TP-only · hold **{payload.get('hold_days')}d**",
        "",
        f"無槽位/資金限制 · 原始 ABC-V3-F1 觸發 · **{payload.get('n_raw_legs')}** legs · "
        f"base gate **{payload.get('n_base_gate_legs')}** legs · "
        f"窗口 **{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"目標 TP-only 均 **≥{target:g}%** · n **≥{min_n}**",
        "",
        "## Base gate",
        "",
        f"- Gap band：`{bg.get('gap_band')}` · poll≥{bg.get('min_poll_idx')}（skip 09:00）· 無 rebound 要求",
        f"- Fixed TP-only：`{payload.get('champion_tp')}` · NEVER_SL",
        "",
        "## Base gate only（no overlay）",
        "",
    ]
    if base and not base.get("skipped"):
        lines.append(
            f"- n={base.get('n')} · hold-only **{base.get('hold_only_mean_pct')}%** · "
            f"TP-only **{base.get('tp_only_mean_pct')}%** · 觸發 **{base.get('tp_fire_rate_pct')}%** · "
            f"≥8% **{base.get('pct_ge_8')}%** · ≥10% **{base.get('pct_ge_10')}%**"
        )
    else:
        lines.append("- （無 base-only cell）")

    lines.extend(["", f"## 目標 ≥{target:g}% · n≥{min_n}", ""])
    if hit:
        lines.append(f"**可達** — {len(hit)} 個 overlay cell 達標：")
        for c in hit[:10]:
            lines.append(
                f"- `{c.get('overlay_label')}` · n={c.get('n')} · TP-only 均 **{c.get('tp_only_mean_pct')}%** "
                f"（hold **{c.get('hold_only_mean_pct')}%** · Δ {c.get('delta_vs_hold_pp')}pp）"
            )
    elif best:
        lines.append(
            f"**未達 {target:g}%** — 最佳 overlay `{best.get('overlay_label')}` · n={best.get('n')} · "
            f"TP-only 均 **{best.get('tp_only_mean_pct')}%**（hold-only **{best.get('hold_only_mean_pct')}%**）"
        )
    else:
        lines.append("無可評估 cell")

    lines.extend(
        [
            "",
            "## Overlay × fixed TP-only（依 TP-only 均報酬排序）",
            "",
            "| Overlay | n | hold-only 均% | TP-only 均% | Δ pp | 觸發% | ≥8% | ≥10% | 達標 |",
            "|---------|--:|--------------:|------------:|-----:|------:|----:|-----:|:----:|",
        ]
    )
    for c in payload.get("cells") or []:
        if c.get("skipped"):
            continue
        flag = "✓" if c.get("hits_target") else "—"
        lines.append(
            f"| {c.get('overlay_label')} | {c.get('n')} | {c.get('hold_only_mean_pct')} | "
            f"{c.get('tp_only_mean_pct')} | {c.get('delta_vs_hold_pp')} | "
            f"{c.get('tp_fire_rate_pct')} | {c.get('pct_ge_8')} | {c.get('pct_ge_10')} | {flag} |"
        )

    lines.extend(
        [
            "",
            "## 設計說明",
            "",
            "- Base gate 為 3 個月窗口 gap sweet-band winner（無 W3 RV rebound 疊加）",
            "- Multi-factor overlay 來自 `w3_rv_hl_winner_profile.FILTER_HYPOTHESES` / `_trade_matches_filter`",
            "- Exit 固定 champion TP-only（gap≥4 + w3 poll_drop + min_ret≥5），無止損",
            "- 對照：同窗口 entry gate × TP sweep（`20260710_abc_v3_f1_entry_gate_tp_sweep_hold5d.md`）",
        ]
    )
    return "\n".join(lines)
