"""ABC v3+F1 · entry gate x TP-only joint sweep · unconstrained universe.

No slot/capital cap: uses every raw ABC-V3-F1 first-trigger stock-day in the
window (not a portfolio-slot-limited ledger). Goal: combine the entry-side MV
gap sweet-band + W3 RV trough rebound gate (abc_v3_f1_entry_structure_sweep)
with the exit-side TP-only rules (abc_v3_f1_kinematic_tp_sl_study) to see
whether a filtered entry universe can push mean hold_days return past target.

Context: both the standalone TP/SL split study and the Gap+RV3 exit study
found a structural ceiling (~6-7% mean on hold_5d) because roughly 2/3 of
legs are structural losers no exit rule can fix (see
20260709_abc_v3_f1_tp_sl_hold5d_90d.md / 20260709_abc_v3_f1_gap_rv3_hold_20260401_20260707.md).
This module tests entry-side quality filtering as the lever instead.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from flow_returns import exit_close_date_from_entry
from market_benchmark import load_benchmark_close
from project_config import DEFAULT_ETF_CODES
from research.backtest.abc_v3_f1_entry_structure_sweep import (
    DEFAULT_GAP_SWEET_BAND,
    EntryStructureParams,
    GapSweetBand,
    Rv3TroughState,
    _rebound_ok,
    _safe_gap,
    _spec_dict,
    build_entry_structure_grid,
)
from research.backtest.archive.abc_v3_f1_kinematic_gap_rv3_study import (
    _leg_from_timeline,
)
from research.backtest.archive.abc_v3_f1_kinematic_tp_sl_study import (
    NEVER_SL,
    TpSlCombo,
    _summarize_tp_sl,
    _tp_grid,
    evaluate_leg_tp_sl,
)
from research.backtest.archive.c18acc_kinematic_timeline_cache import (
    _build_kinematic_timeline,
)
from research.backtest.archive.c18acc_s2_dual_wma_annot import (
    _collect_annot_dates,
    _exit_frame_idx_for_date,
)
from research.backtest.archive.c18acc_triple_wma_extreme_profile import (
    _frame_idx_for_minute,
)
from research.backtest.dual_wma_signal_backtest import _build_stock_frame_maps, _price_at
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.triple_wma_pullback_sweep import _mv, _rv, matches_w3_rv_hl_abc_v3_f1
from research.backtest.w3_rv_hl_winner_profile import _poll_idx_for_frame
from rrg_universe_intraday_panel import (
    WMA_MICRO_LENGTH,
    WMA_ULTRA_LENGTH,
    build_dual_wma_intraday_trajectories,
    intraday_poll_minutes,
)


def collect_abc_v3_f1_raw_legs(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    hold_days: int = 5,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> list[dict[str, Any]]:
    """Raw ABC-V3-F1 first-trigger events (no slot cap) -> hold_days kinematic
    legs, each annotated with pre-entry gate features (gate_gap_w20_w5,
    gate_gap_w5_w3, gate_rv3_rebound, gate_rv3_poll_rise, gate_poll_idx)."""
    frames_a, trajectories_a, _ = build_dual_wma_intraday_trajectories(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
    )
    frame_ids_a = [f["id"] for f in frames_a]
    events: list[dict[str, Any]] = []
    for t in trajectories_a:
        sid = t["stock_id"]
        by_id = {p["frame_id"]: p for p in t["points"]}
        trough = Rv3TroughState()
        seen_day: set[str] = set()
        for i, fid in enumerate(frame_ids_a):
            poll_idx = _poll_idx_for_frame(frames_a, i)
            if poll_idx is None:
                continue
            p = by_id.get(fid)
            if not p:
                continue
            if poll_idx == 0:
                trough.reset()
            rv3 = _rv(p, "w3")
            rebound: float | None = None
            poll_rise = False
            if rv3 is not None:
                rebound, poll_rise = trough.update(rv3)
            if not matches_w3_rv_hl_abc_v3_f1(p):
                continue
            d = frames_a[i]["date"]
            if d in seen_day:
                continue
            seen_day.add(d)
            events.append(
                {
                    "stock_id": sid,
                    "entry_date": d,
                    "entry_minute": str(p.get("minute") or frames_a[i].get("minute") or ""),
                    "poll_idx": poll_idx,
                    "gap_w20_w5": _safe_gap(_mv(p, "w20"), _mv(p, "w5")),
                    "gap_w5_w3": _safe_gap(_mv(p, "w5"), _mv(p, "w3")),
                    "rv3_rebound": rebound,
                    "rv3_poll_rise": bool(poll_rise),
                }
            )
    if not events:
        return []

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    periods: list[dict[str, Any]] = []
    for ev in events:
        exit_d = exit_close_date_from_entry(conn, ev["entry_date"], hold_days)
        if not exit_d:
            continue
        periods.append({**ev, "exit_date": exit_d})
    if not periods:
        return []

    stock_ids = sorted({str(p["stock_id"]) for p in periods})
    annot_dates = _collect_annot_dates(periods, full_dates)
    if not annot_dates:
        return []

    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    frames, trajectories, _ = build_dual_wma_intraday_trajectories(
        conn,
        dates=annot_dates,
        stock_ids=stock_ids,
        poll_minutes=intraday_poll_minutes(),
        micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
        kbar_bar_minutes=5,
    )
    stock_maps = _build_stock_frame_maps(trajectories, frames)

    legs: list[dict[str, Any]] = []
    for period in periods:
        sid = str(period["stock_id"])
        entry = str(period["entry_date"])
        exit_d = str(period["exit_date"])
        points = stock_maps.get(sid)
        if not points:
            continue
        entry_i = _frame_idx_for_minute(frames, entry, period.get("entry_minute"))
        exit_i = _exit_frame_idx_for_date(frames, exit_d)
        if exit_i < entry_i:
            continue
        entry_px, _ = _price_at(conn, bench, sid, entry, str(period.get("entry_minute") or ""))
        if not entry_px or entry_px <= 0:
            continue
        timeline = _build_kinematic_timeline(
            conn,
            bench,
            stock_id=sid,
            entry_i=entry_i,
            exit_i=exit_i,
            frames=frames,
            points_by_idx=points,
            entry_px=float(entry_px),
        )
        leg = _leg_from_timeline(
            {"entry_date": entry, "stock_id": sid, "exit_date": exit_d, "slot": 0},
            timeline,
            hold_days_cap=hold_days,
        )
        if not leg:
            continue
        leg["gate_poll_idx"] = period["poll_idx"]
        leg["gate_gap_w20_w5"] = period["gap_w20_w5"]
        leg["gate_gap_w5_w3"] = period["gap_w5_w3"]
        leg["gate_rv3_rebound"] = period["rv3_rebound"]
        leg["gate_rv3_poll_rise"] = period["rv3_poll_rise"]
        legs.append(leg)
    return legs


def _cell_matches_leg(cell: EntryStructureParams, leg: dict[str, Any]) -> bool:
    poll_idx = leg.get("gate_poll_idx")
    if poll_idx is None or int(poll_idx) < cell.min_poll_idx:
        return False
    gap20_5 = leg.get("gate_gap_w20_w5")
    gap5_3 = leg.get("gate_gap_w5_w3")
    if gap20_5 is None or gap5_3 is None:
        return False
    band = cell.gap_band
    if not (band.gap_w20_w5_min <= float(gap20_5) <= band.gap_w20_w5_max):
        return False
    if not (band.gap_w5_w3_min <= float(gap5_3) <= band.gap_w5_w3_max):
        return False
    rebound = leg.get("gate_rv3_rebound")
    if rebound is None:
        return False
    return _rebound_ok(
        float(rebound),
        bool(leg.get("gate_rv3_poll_rise")),
        rebound_min=cell.rebound_min,
        mode=cell.rebound_mode,
    )


def default_joint_sweep_grid() -> tuple[EntryStructureParams, ...]:
    bands = (
        DEFAULT_GAP_SWEET_BAND,
        GapSweetBand(gap_w20_w5_min=2.0, gap_w20_w5_max=8.0, gap_w5_w3_min=-1.0, gap_w5_w3_max=2.0),
        GapSweetBand(gap_w20_w5_min=3.0, gap_w20_w5_max=6.0, gap_w5_w3_min=-0.5, gap_w5_w3_max=1.0),
    )
    return build_entry_structure_grid(
        gap_bands=bands,
        rebound_mins=(0.0, 0.2, 0.3, 0.5, 0.8),
        rebound_modes=("rebound_only", "rebound_and_poll_rise"),
        min_poll_idxs=(0, 1),
    )


def _best_tp_only(legs: list[dict[str, Any]], *, variant_prefix: str) -> tuple[dict[str, Any], dict[str, Any]]:
    hold_only = _summarize_tp_sl(
        legs,
        [{"fired": False, "exit_ret_pct": float(l["return_pct"])} for l in legs],
        variant_id=f"{variant_prefix}_hold_only",
    )
    rows: list[dict[str, Any]] = []
    for tp in _tp_grid():
        combo = TpSlCombo(tp=tp, sl=NEVER_SL)
        results = [evaluate_leg_tp_sl(leg, combo) for leg in legs]
        rows.append(_summarize_tp_sl(legs, results, variant_id=tp.variant_id, combo=combo))
    rows.sort(key=lambda s: -(float(s.get("mean_all_legs_ret_pct") or 0)))
    best = rows[0] if rows else {}
    return hold_only, best


def run_entry_gate_tp_joint_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2026-04-01",
    date_end: str = "2026-07-07",
    hold_days: int = 5,
    target_pct: float = 10.0,
    min_leg_n: int = 15,
    entry_grid: tuple[EntryStructureParams, ...] | None = None,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    sample_dates = [d for d in full_dates if date_start <= d <= date_end]
    if not sample_dates:
        raise ValueError(f"no trading dates in [{date_start}, {date_end}]")

    legs = collect_abc_v3_f1_raw_legs(
        conn, dates=sample_dates, hold_days=hold_days, etf_codes=etf_codes
    )
    if not legs:
        raise ValueError("no raw ABC-V3-F1 legs in window")

    all_hold_only, all_best_tp = _best_tp_only(legs, variant_prefix="all")

    grid = entry_grid or default_joint_sweep_grid()
    cells: list[dict[str, Any]] = []
    for cell in grid:
        filtered = [leg for leg in legs if _cell_matches_leg(cell, leg)]
        n = len(filtered)
        if n < min_leg_n:
            cells.append({**_spec_dict(cell), "n": n, "skipped": True})
            continue
        hold_only, best_tp = _best_tp_only(filtered, variant_prefix=cell.id)
        best_ret = best_tp.get("mean_all_legs_ret_pct")
        cells.append(
            {
                **_spec_dict(cell),
                "n": n,
                "skipped": False,
                "hold_only_mean_pct": hold_only.get("mean_all_legs_ret_pct"),
                "best_tp_only_mean_pct": best_ret,
                "best_tp_only_variant": best_tp.get("variant_id"),
                "best_tp_only_fire_rate_pct": best_tp.get("fire_rate_pct"),
                "hits_target": bool(best_ret is not None and float(best_ret) >= target_pct),
            }
        )
    cells.sort(key=lambda c: float(c.get("best_tp_only_mean_pct") or -999), reverse=True)
    best_cell = next((c for c in cells if not c.get("skipped")), None)
    hit_cells = [c for c in cells if c.get("hits_target")]

    return {
        "schema": "abc_v3_f1_entry_gate_tp_sweep-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": date_end,
        "hold_days": hold_days,
        "target_pct": target_pct,
        "min_leg_n": min_leg_n,
        "n_raw_legs": len(legs),
        "all_hold_only": all_hold_only,
        "all_best_tp_only": all_best_tp,
        "cells": cells,
        "best_cell": best_cell,
        "cells_hitting_target": hit_cells,
    }


def render_entry_gate_tp_joint_sweep_md(payload: dict[str, Any]) -> str:
    target = float(payload.get("target_pct") or 10.0)
    all_hold = payload.get("all_hold_only") or {}
    all_tp = payload.get("all_best_tp_only") or {}
    best = payload.get("best_cell") or {}
    hit = payload.get("cells_hitting_target") or []
    lines = [
        f"# ABC v3+F1 · entry gate × TP-only joint sweep · hold **{payload.get('hold_days')}d**",
        "",
        f"無槽位/資金限制 · 原始 ABC-V3-F1 觸發全樣本 · **{payload.get('n_raw_legs')}** legs · "
        f"窗口 **{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"目標 portfolio 均 **≥{target:g}%**",
        "",
        "## 未過濾（unconstrained universe）baseline",
        "",
        f"- Hold only：均 **{all_hold.get('mean_all_legs_ret_pct')}%** · "
        f"≥{target:g}% 占比 **{all_hold.get('pct_legs_hit_10' , '—')}%**",
        f"- 最佳 TP-only：均 **{all_tp.get('mean_all_legs_ret_pct')}%** · "
        f"觸發 **{all_tp.get('fire_rate_pct')}%** · `{all_tp.get('variant_id')}`",
        "",
        f"## 目標 ≥{target:g}%",
        "",
    ]
    if hit:
        lines.append(f"**可達** — {len(hit)} 個 entry gate cell 疊加最佳 TP-only ≥ {target:g}%：")
        for c in hit[:10]:
            lines.append(
                f"- `{c.get('label')}` · n={c.get('n')} · 均 **{c.get('best_tp_only_mean_pct')}%** "
                f"(`{c.get('best_tp_only_variant')}`)"
            )
    elif best:
        lines.append(
            f"**未達 {target:g}%** — 最佳 entry gate `{best.get('label')}` · n={best.get('n')} · "
            f"均 **{best.get('best_tp_only_mean_pct')}%**（hold-only **{best.get('hold_only_mean_pct')}%**）"
        )
    else:
        lines.append(f"無 entry gate cell 達到 min_leg_n={payload.get('min_leg_n')}")

    lines.extend(
        [
            "",
            "## Entry gate × TP-only（依最佳 TP-only 均報酬排序，Top 20）",
            "",
            "| Cell | n | hold-only 均% | best TP-only 均% | 觸發% | ≥target |",
            "|------|--:|--------------:|------------------:|------:|:-------:|",
        ]
    )
    shown = [c for c in (payload.get("cells") or []) if not c.get("skipped")][:20]
    for c in shown:
        lines.append(
            f"| {c.get('label')} | {c.get('n')} | {c.get('hold_only_mean_pct')} | "
            f"{c.get('best_tp_only_mean_pct')} | {c.get('best_tp_only_fire_rate_pct')} | "
            f"{'✓' if c.get('hits_target') else '—'} |"
        )
    skipped_n = sum(1 for c in (payload.get("cells") or []) if c.get("skipped"))
    if skipped_n:
        lines.extend(["", f"（另有 {skipped_n} 個 cell 未達 min_leg_n={payload.get('min_leg_n')}，已略過 TP sweep）"])

    lines.extend(
        [
            "",
            "## 設計說明",
            "",
            "- 樣本為**原始訊號事件**（每個 stock-day 第一次 ABC-V3-F1 觸發），不套用槽位/資金分配",
            "- Entry gate：MV gap sweet-band（W20−W5、W5−W3）× W3 RV 谷底反彈 δ × 進場 poll 下限",
            "- Exit：TP-only 掃描（`gap_rv` / `rv_only` / `trail_giveback`），無止損（ABC 上止損多半有害，"
            "見 TP/SL 分離研究）",
            f"- 對照：`{payload.get('date_start')}`..`{payload.get('date_end')}` 同窗口的槽位限制版本"
            "（`20260709_abc_v3_f1_tp_sl_hold5d_full.md`）僅達 5.9% 均報酬",
        ]
    )
    return "\n".join(lines)
