"""C18acc rotation-exit · Track A swap rule variant sweep."""

from __future__ import annotations

import sqlite3
from typing import Any

import pandas as pd

from market_benchmark import load_benchmark_close
from research.backtest.c18acc_swap_block_audit import (
    WATCH_STOCKS,
    _days_to_rotation,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, LENGTH
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    close_champion_score_swap_c_config,
    simulate_score_swap_c,
)
from rrg_rotation import compute_rrg_panel

ROTATION_EXIT_VARIANTS: list[ScoreSwapCConfig] = []


def _variant_from_base(
    variant_id: str,
    label: str,
    **overrides: Any,
) -> ScoreSwapCConfig:
    base = close_champion_score_swap_c_config()
    fields = base.to_dict()
    fields["variant_id"] = variant_id
    fields["label"] = label
    fields.update(overrides)
    return ScoreSwapCConfig(**fields)


def rotation_exit_sweep_configs() -> list[ScoreSwapCConfig]:
    """Track A · A0–A5 swap rule variants."""
    return [
        _variant_from_base("A0", "baseline · champion close"),
        _variant_from_base("A1", "accel_sell_negative_only=False", accel_sell_negative_only=False),
        _variant_from_base("A2", "min_hold_days=4", min_hold_days=4),
        _variant_from_base(
            "A3",
            "quad_weakening_2d sell eligibility",
            quad_weakening_sell_days=2,
        ),
        _variant_from_base(
            "A4",
            "quad_lagging_1d sell eligibility",
            quad_lagging_sell_days=1,
        ),
        _variant_from_base(
            "A5",
            "loss_waiver threshold=-5% min_hold=2",
            loss_waiver_threshold=-0.05,
            loss_waiver_min_hold=2,
        ),
    ]


def _rotation_cohort_periods(
    periods: list[dict[str, Any]],
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in periods:
        dtr = _days_to_rotation(
            rs_ratio,
            rs_mom,
            full_dates,
            entry_date=str(p["entry_date"]),
            exit_date=str(p["exit_date"]),
            stock_id=str(p["stock_id"]),
        )
        if dtr is not None and dtr <= 5 and float(p.get("return_pct") or 0) <= -10.0:
            out.append(p)
    return out


def _cohort_stats(periods: list[dict[str, Any]]) -> dict[str, Any]:
    if not periods:
        return {"n": 0, "mean_return_pct": None, "mean_excess_pct": None, "mean_save_vs_entry_pp": None}
    rets = [float(p.get("return_pct") or 0) for p in periods]
    exc = [float(p.get("excess_pct") or 0) for p in periods]
    return {
        "n": len(periods),
        "mean_return_pct": round(sum(rets) / len(rets), 4),
        "mean_excess_pct": round(sum(exc) / len(exc), 4),
    }


def run_c18acc_rotation_exit_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-30",
    configs: list[ScoreSwapCConfig] | None = None,
) -> dict[str, Any]:
    from market_breadth_ma import build_breadth_panel

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    grid = configs or rotation_exit_sweep_configs()
    kbar_cache: dict = {}
    rows: list[dict[str, Any]] = []
    baseline_rotation: dict[str, Any] | None = None
    baseline_summary: dict[str, Any] | None = None
    a0_periods: list[dict[str, Any]] = []

    for cfg in grid:
        print(f"rotation exit sweep · {cfg.variant_id} · {cfg.label} ...", flush=True)
        periods, summary = simulate_score_swap_c(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            bench=bench,
            fresh_by_date=fresh_by_date,
            zone_by_date=zone_by_date,
            config=cfg,
            kbar_cache=kbar_cache,
            rs_mom=rs_mom,
            rs_ratio=rs_ratio,
            entry_c_config=c0_cfg,
        )
        rot = _rotation_cohort_periods(periods, rs_ratio, rs_mom, full_dates)
        rot_stats = _cohort_stats(rot)
        row = {
            "variant_id": cfg.variant_id,
            "label": cfg.label,
            "mean_excess_pct": summary.get("mean_excess_pct"),
            "swaps_total": summary.get("swaps_total"),
            "n_periods": summary.get("n_periods"),
            "rotation_cohort": rot_stats,
            "config": cfg.to_dict(),
        }
        if cfg.variant_id == "A0":
            baseline_rotation = rot_stats
            baseline_summary = summary
            a0_periods = periods
            row["delta_vs_a0_mean_excess_pp"] = 0.0
            row["rotation_cohort_delta_mean_return_pp"] = 0.0
        else:
            base_ex = (baseline_summary or {}).get("mean_excess_pct")
            ex = summary.get("mean_excess_pct")
            row["delta_vs_a0_mean_excess_pp"] = (
                round(float(ex) - float(base_ex), 4) if ex is not None and base_ex is not None else None
            )
            br = (baseline_rotation or {}).get("mean_return_pct")
            rr = rot_stats.get("mean_return_pct")
            row["rotation_cohort_delta_mean_return_pp"] = (
                round(float(rr) - float(br), 4) if rr is not None and br is not None else None
            )
        rows.append(row)
        print(
            f"  {cfg.variant_id}: excess={summary.get('mean_excess_pct')}% "
            f"swaps={summary.get('swaps_total')} rot_n={rot_stats.get('n')}",
            flush=True,
        )

    ranked = sorted(rows, key=lambda r: (-(r.get("mean_excess_pct") or -999.0), r.get("variant_id", "")))
    best = ranked[0] if ranked else None

    track_c: dict[str, Any] | None = None
    if a0_periods:
        from research.backtest.c18acc_extension_exit import apply_rotation_exit_overlay

        c1_periods, c1_summary = apply_rotation_exit_overlay(
            conn,
            base_periods=a0_periods,
            full_dates=full_dates,
            close=close,
            rs_ratio=rs_ratio,
            rs_mom=rs_mom,
            min_hold_days=5,
            target_quad="lagging",
        )
        c1_rot = _rotation_cohort_periods(c1_periods, rs_ratio, rs_mom, full_dates)
        a0_rot = _rotation_cohort_periods(a0_periods, rs_ratio, rs_mom, full_dates)
        a0_rot_stats = _cohort_stats(a0_rot)
        c1_rot_stats = _cohort_stats(c1_rot)
        base_ex = (baseline_summary or {}).get("mean_excess_pct")
        c1_ex = c1_summary.get("mean_excess_pct")
        track_c = {
            "variant_id": "C1",
            "label": "force exit at close on lagging after min_hold",
            "summary": c1_summary,
            "rotation_cohort": c1_rot_stats,
            "rotation_cohort_baseline": a0_rot_stats,
            "delta_vs_a0_mean_excess_pp": (
                round(float(c1_ex) - float(base_ex), 4) if c1_ex is not None and base_ex is not None else None
            ),
            "rotation_cohort_delta_mean_return_pp": (
                round(float(c1_rot_stats["mean_return_pct"]) - float(a0_rot_stats["mean_return_pct"]), 4)
                if c1_rot_stats.get("mean_return_pct") is not None
                and a0_rot_stats.get("mean_return_pct") is not None
                else None
            ),
        }

    return {
        "topic": "c18acc-rotation-exit",
        "track": "A",
        "date_start": date_start,
        "date_end": date_end,
        "baseline_variant": "A0",
        "watch_stocks": list(WATCH_STOCKS),
        "summaries": rows,
        "best_by_mean_excess": best,
        "track_c": track_c,
        "hypotheses": {
            "H-ROT-A1": "accel_sell_negative_only=False 減少 rotation cohort 大虧",
            "H-ROT-A2": "min_hold=4 提早換倉不傷全樣本均超額",
            "H-ROT-A3": "quad_weakening_2d 對 gradual rotation 有效",
            "H-ROT-A4": "quad_lagging_1d 對 rotation cohort save > A0",
            "H-ROT-A5": "loss_waiver -5% 縮短 min_hold 減 rotation 大虧",
        },
    }


def render_c18acc_rotation_exit_sweep_md(payload: dict[str, Any]) -> str:
    lines = [
        "# C18acc · Track A Rotation Exit Sweep",
        "",
        f"區間：{payload.get('date_start')} .. {payload.get('date_end')}",
        "",
        "| id | label | mean_excess% | swaps | n | rot_n | rot_mean_ret% | Δexcess vs A0 | Δrot_ret vs A0 |",
        "|----|-------|--------------|-------|---|-------|---------------|---------------|----------------|",
    ]
    for s in payload.get("summaries") or []:
        rot = s.get("rotation_cohort") or {}
        lines.append(
            f"| {s.get('variant_id')} | {s.get('label')} "
            f"| {s.get('mean_excess_pct')} | {s.get('swaps_total')} | {s.get('n_periods')} "
            f"| {rot.get('n')} | {rot.get('mean_return_pct')} "
            f"| {s.get('delta_vs_a0_mean_excess_pp')} | {s.get('rotation_cohort_delta_mean_return_pp')} |"
        )
    best = payload.get("best_by_mean_excess") or {}
    if best:
        lines += [
            "",
            "## Best by mean excess",
            "",
            f"- **{best.get('variant_id')}** · {best.get('label')}",
            f"- mean_excess={best.get('mean_excess_pct')}% · Δ vs A0={best.get('delta_vs_a0_mean_excess_pp')}pp",
        ]
    tc = payload.get("track_c") or {}
    if tc:
        rot = tc.get("rotation_cohort") or {}
        lines += [
            "",
            "## Track C · rotation overlay",
            "",
            f"- **{tc.get('variant_id')}** · {tc.get('label')}",
            f"- triggers={tc.get('summary', {}).get('overlay_triggers')} · "
            f"mean_excess={tc.get('summary', {}).get('mean_excess_pct')}% · "
            f"Δ vs A0={tc.get('delta_vs_a0_mean_excess_pp')}pp",
            f"- rotation cohort n={rot.get('n')} · mean_ret={rot.get('mean_return_pct')}% · "
            f"Δ rot_ret vs A0={tc.get('rotation_cohort_delta_mean_return_pp')}pp",
        ]
    lines += [
        "",
        "---",
        "模組：`scripts/run_c18acc_rotation_exit_sweep.py`",
        "",
    ]
    return "\n".join(lines)
