"""ABC v3+f1 · Gap OR + RV3 exit · hold 3/4/5d counterfactual.

Builds kinematic timelines from comparison ledger (same path as lead-lag study).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from flow_returns import exit_close_date_from_entry
from market_benchmark import load_benchmark_close
from research.backtest.archive.abc_v3_f1_kinematic_leadlag_study import (
    _ledger_row_to_period,
    resolve_ledger_json,
)
from research.backtest.archive.abc_v3_f1_overnight_regime_annot import (
    load_abc_f1_trade_ledger_from_json,
)
from research.backtest.archive.c18acc_kinematic_timeline_cache import (
    KinematicSnap,
    _build_kinematic_timeline,
)
from research.backtest.archive.c18acc_s2_dual_wma_annot import (
    _collect_annot_dates,
    _exit_frame_idx_for_date,
)
from research.backtest.archive.c18acc_triple_wma_extreme_profile import (
    _frame_idx_for_minute,
)
from research.backtest.archive.c18acc_kinematic_gap_rv3_study import (
    CHAMPION_GAP_RV3,
    GapRv3Params,
    evaluate_leg_gap_rv3,
    _pct_hit_target,
    _summarize_portfolio,
)
from research.backtest.dual_wma_signal_backtest import _build_stock_frame_maps
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.w3_rv_hl_winner_profile import _mean
from rrg_universe_intraday_panel import (
    WMA_MICRO_LENGTH,
    WMA_ULTRA_LENGTH,
    build_dual_wma_intraday_trajectories,
    intraday_poll_minutes,
)

SCHEMA = "abc_v3_f1_kinematic_gap_rv3-v1"


def _leg_from_timeline(
    period: dict[str, Any],
    timeline: list[KinematicSnap],
    *,
    hold_days_cap: int | None = None,
) -> dict[str, Any] | None:
    if len(timeline) < 2:
        return None
    peak_i = max(range(len(timeline)), key=lambda i: timeline[i].ret_from_entry_pct)
    peak = timeline[peak_i]
    hold_ret = round(float(timeline[-1].ret_from_entry_pct), 3)
    leg_id = f"{period.get('entry_date')}|{period.get('stock_id')}|{period.get('slot', 0)}"
    return {
        "leg_id": leg_id,
        "stock_id": period.get("stock_id"),
        "entry_date": period.get("entry_date"),
        "exit_date": period.get("exit_date"),
        "hold_days_cap": hold_days_cap,
        "return_pct": hold_ret,
        "outcome": "win" if hold_ret > 0 else "loss",
        "peak_ret_pct": round(float(peak.ret_from_entry_pct), 3),
        "peak_frame_idx": peak_i,
        "entry_t0": timeline[0].to_dict(),
        "timeline": [s.to_dict() for s in timeline],
    }


def build_abc_f1_kinematic_legs(
    conn: sqlite3.Connection,
    ledger: list[dict[str, Any]],
    *,
    hold_days: int = 3,
) -> list[dict[str, Any]]:
    """Build legs with explicit ``hold_days`` exit (3/4/5 trading days)."""
    periods = [_ledger_row_to_period(r) for r in ledger]
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    stock_ids = sorted({str(p["stock_id"]) for p in periods})
    annot_dates = _collect_annot_dates(periods, full_dates)
    if not annot_dates:
        return []

    print(
        f"abc-f1-gap-rv3: {len(ledger)} ledger · hold={hold_days}d · "
        f"{len(stock_ids)} stocks · {len(annot_dates)} dates …",
        flush=True,
    )
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
    for row in ledger:
        period = _ledger_row_to_period(row)
        sid = str(period["stock_id"])
        entry = str(period["entry_date"])
        entry_px = period.get("entry_px")
        if not entry_px or float(entry_px) <= 0:
            continue
        exit_d = exit_close_date_from_entry(conn, entry, hold_days)
        if not exit_d:
            continue
        period = {**period, "exit_date": exit_d, "hold_days": hold_days}
        points = stock_maps.get(sid)
        if not points:
            continue
        entry_i = _frame_idx_for_minute(frames, entry, period.get("entry_minute"))
        exit_i = _exit_frame_idx_for_date(frames, exit_d)
        if exit_i < entry_i:
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
        leg = _leg_from_timeline(period, timeline, hold_days_cap=hold_days)
        if leg:
            legs.append(leg)
    return legs


def run_abc_f1_gap_rv3_hold_study(
    conn: sqlite3.Connection,
    *,
    ledger_json: Path | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    hold_days_list: tuple[int, ...] = (3, 4, 5),
    target_pct: float = 10.0,
) -> dict[str, Any]:
    ledger_path = resolve_ledger_json(ledger_json)
    ledger, ledger_meta = load_abc_f1_trade_ledger_from_json(ledger_path)
    if date_start:
        ledger = [r for r in ledger if str(r.get("entry_date") or "") >= date_start]
    if date_end:
        ledger = [r for r in ledger if str(r.get("entry_date") or "") <= date_end]
    if not ledger:
        raise ValueError("no ledger rows after filter")

    by_hold: dict[str, Any] = {}
    min_ret_grid = (8.0, 10.0, 12.0, 15.0)
    rv_modes = ("drv_and_poll", "poll_drop", "drv_or_poll")

    for hd in hold_days_list:
        legs = build_abc_f1_kinematic_legs(conn, ledger, hold_days=hd)
        if not legs:
            continue
        hold_row = _summarize_portfolio(
            legs,
            [{"fired": False, "exit_ret_pct": float(l["return_pct"])} for l in legs],
            variant_id=f"hold_{hd}d",
        )
        hold_row["pct_legs_hit_10"] = _pct_hit_target(legs, target_pct)

        rule_rows: list[dict[str, Any]] = []
        for min_ret in min_ret_grid:
            for rv_mode in rv_modes:
                for gw5 in (6.0, 7.0, 8.0):
                    p = GapRv3Params(
                        gap_logic="or",
                        gw5_min=gw5,
                        gw3_min=2.0,
                        gap_expand_min=0.0,
                        rv3_mode=rv_mode,
                        drv3_min=0.45 if rv_mode != "poll_drop" else 0.0,
                        dmv3_min=0.0,
                        min_ret_pct=min_ret,
                        require_pullback=False,
                        dist_min=0.0,
                        consecutive=1,
                    )
                    results = [evaluate_leg_gap_rv3(leg, p) for leg in legs]
                    s = _summarize_portfolio(legs, results, variant_id=p.variant_id, params=p)
                    s["pct_legs_hit_10"] = _pct_hit_target(
                        [{"return_pct": r["exit_ret_pct"]} for r in results], target_pct
                    )
                    rule_rows.append(s)

        rule_rows.sort(key=lambda s: -(float(s.get("mean_all_legs_ret_pct") or 0)))
        best = rule_rows[0] if rule_rows else {}
        hit_target = [r for r in rule_rows if float(r.get("mean_all_legs_ret_pct") or 0) >= target_pct]

        from dataclasses import replace

        champ = replace(CHAMPION_GAP_RV3, min_ret_pct=10.0)
        champ_res = _summarize_portfolio(
            legs,
            [evaluate_leg_gap_rv3(leg, champ) for leg in legs],
            variant_id="champion_min10",
            params=champ,
        )
        champ_res["pct_legs_hit_10"] = _pct_hit_target(
            [{"return_pct": evaluate_leg_gap_rv3(leg, champ)["exit_ret_pct"]} for leg in legs],
            target_pct,
        )

        by_hold[str(hd)] = {
            "hold_days": hd,
            "n_legs": len(legs),
            "hold_only": hold_row,
            "best_rule": best,
            "champion_min_ret10": champ_res,
            "rules_at_or_above_target": hit_target[:5],
            "top_rules": rule_rows[:10],
        }

    return {
        "schema": SCHEMA,
        "ledger_source": str(ledger_path),
        "ledger_meta": ledger_meta,
        "date_start": date_start,
        "date_end": date_end,
        "n_ledger_rows": len(ledger),
        "target_pct": target_pct,
        "hold_days_list": list(hold_days_list),
        "by_hold_days": by_hold,
        "abc_pipeline_reference": {
            "f1_90d_event_mean_pct": 2.845,
            "f1_val_mean_pct": 1.826,
            "note": "Pipeline event-level · not identical to this ledger slice",
        },
    }


def render_abc_f1_gap_rv3_hold_study_md(payload: dict[str, Any]) -> str:
    target = float(payload.get("target_pct") or 10.0)
    ref = payload.get("abc_pipeline_reference") or {}
    lines = [
        "# ABC v3+f1 · Gap OR + RV3 × hold 3/4/5d",
        "",
        f"Ledger：`{Path(str(payload.get('ledger_source', ''))).name}` · "
        f"窗口 **{payload.get('date_start') or '—'}** .. **{payload.get('date_end') or '—'}** · "
        f"目標 **{target:g}%** portfolio 均報酬",
        "",
        f"Pipeline 参照（90d event f1）：均 **{ref.get('f1_90d_event_mean_pct')}%** · "
        f"val **{ref.get('f1_val_mean_pct')}%**",
        "",
    ]
    for hd in sorted((payload.get("by_hold_days") or {}).keys(), key=int):
        block = payload["by_hold_days"][hd]
        hold = block.get("hold_only") or {}
        best = block.get("best_rule") or {}
        champ = block.get("champion_min_ret10") or {}
        lines.extend(
            [
                f"## Hold **{hd}** 天 · n={block.get('n_legs')}",
                "",
                f"- **Hold only**：均 **{hold.get('mean_all_legs_ret_pct')}%** · "
                f"≥{target:g}% 占比 **{hold.get('pct_legs_hit_10')}%**",
                f"- **最佳 Gap+RV3**：`{best.get('variant_id')}` · 均 **{best.get('mean_all_legs_ret_pct')}%** "
                f"(Δ {best.get('delta_vs_hold_pp')}pp) · 觸發 {best.get('fire_rate_pct')}%",
                f"- **Champion min10**：均 **{champ.get('mean_all_legs_ret_pct')}%** · 觸發 {champ.get('fire_rate_pct')}%",
                "",
            ]
        )
        above = block.get("rules_at_or_above_target") or []
        if above:
            lines.append(f"均報酬 ≥ {target:g}% 的規則：")
            for r in above:
                lines.append(f"- `{r.get('variant_id')}` · **{r.get('mean_all_legs_ret_pct')}%**")
            lines.append("")
        lines.extend(
            [
                "| # | variant | 全 leg 均 | Δ | 觸發率 | ≥10% |",
                "|---|---------|---------|---|--------|------|",
            ]
        )
        for i, r in enumerate((block.get("top_rules") or [])[:5], 1):
            lines.append(
                f"| {i} | `{r.get('variant_id')}` | **{r.get('mean_all_legs_ret_pct')}%** | "
                f"{r.get('delta_vs_hold_pp')}pp | {r.get('fire_rate_pct')}% | {r.get('pct_legs_hit_10')}% |"
            )
        lines.append("")

    best_val = 0.0
    best_hd = ""
    for hd, block in (payload.get("by_hold_days") or {}).items():
        v = float((block.get("best_rule") or {}).get("mean_all_legs_ret_pct") or 0)
        if v > best_val:
            best_val, best_hd = v, hd

    lines.extend(
        [
            "## 結論",
            "",
            f"- ABC v3+f1 **baseline ~2–3%**/筆（3d pipeline）· 本研究最佳 portfolio 均 **{best_val:.2f}%**（hold {best_hd}d）",
            f"- **{target:g}% portfolio 均報酬**："
            + ("**可達**" if best_val >= target else f"**未達**（距目標 {(target - best_val):.1f}pp）"),
            "- Gap+RV3 在 ABC 上同樣主要提升 **觸發 leg 鎖利** · 延長至 4/5d 需看 hold-only 是否上升",
            "- 10% 更 realistic 作 **單筆止盈**（min_ret≥10）· 非全池均値",
        ]
    )
    return "\n".join(lines)
