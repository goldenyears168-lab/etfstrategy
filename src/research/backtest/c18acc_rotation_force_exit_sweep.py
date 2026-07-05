"""C18acc rotation-exit · Track B quad force-exit sweep."""

from __future__ import annotations

import sqlite3
from typing import Any

import pandas as pd

from market_benchmark import load_benchmark_close
from research.backtest.c18acc_swap_block_audit import (
    WATCH_STOCKS,
    _days_to_rotation,
)
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, LENGTH
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    close_champion_score_swap_c_config,
    simulate_score_swap_c,
)

from rrg_rotation import compute_rrg_panel


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


def rotation_force_exit_sweep_configs() -> list[ScoreSwapCConfig]:
    """Track B · B0–B5 force-exit variants (B6 = B1 + I36 overlay at runtime)."""
    return [
        _variant_from_base("B0", "baseline · champion close (A0)"),
        _variant_from_base(
            "B1",
            "force exit · 1d lagging after min_hold",
            quad_force_exit_mode="lagging",
            quad_force_exit_min_days=1,
        ),
        _variant_from_base(
            "B2",
            "force exit · 2d weakening consecutive",
            quad_force_exit_mode="weakening",
            quad_force_exit_min_days=2,
        ),
        _variant_from_base(
            "B3",
            "force exit · 1d weakening after min_hold",
            quad_force_exit_mode="weakening",
            quad_force_exit_min_days=1,
        ),
        _variant_from_base(
            "B4",
            "force exit · weakening_or_lagging 1d",
            quad_force_exit_mode="weakening_or_lagging",
            quad_force_exit_min_days=1,
        ),
        _variant_from_base(
            "B5",
            "B2 + accel_sell_negative_only=False",
            quad_force_exit_mode="weakening",
            quad_force_exit_min_days=2,
            accel_sell_negative_only=False,
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
        return {"n": 0, "mean_return_pct": None, "mean_excess_pct": None}
    rets = [float(p.get("return_pct") or 0) for p in periods]
    exc = [float(p.get("excess_pct") or 0) for p in periods]
    return {
        "n": len(periods),
        "mean_return_pct": round(sum(rets) / len(rets), 4),
        "mean_excess_pct": round(sum(exc) / len(exc), 4),
    }


def _leg_key(p: dict[str, Any]) -> str:
    return f"{p.get('stock_id')}|{p.get('entry_date')}"


def _watch_stock_counterfactual(
    b0_periods: list[dict[str, Any]],
    variant_periods: list[dict[str, Any]],
    *,
    variant_id: str,
) -> list[dict[str, Any]]:
    b0_by_key = {_leg_key(p): p for p in b0_periods if str(p.get("stock_id")) in WATCH_STOCKS}
    var_by_key = {_leg_key(p): p for p in variant_periods if str(p.get("stock_id")) in WATCH_STOCKS}
    rows: list[dict[str, Any]] = []
    for key, b0 in b0_by_key.items():
        var = var_by_key.get(key)
        b0_ret = float(b0.get("return_pct") or 0)
        var_ret = float(var.get("return_pct") or 0) if var else None
        force_exit = var is not None and var.get("exit_reason") == "quad_force_exit"
        rows.append(
            {
                "stock_id": b0.get("stock_id"),
                "entry_date": b0.get("entry_date"),
                "b0_exit_date": b0.get("exit_date"),
                "b0_exit_reason": b0.get("exit_reason"),
                "b0_return_pct": b0_ret,
                "variant_id": variant_id,
                "variant_exit_date": var.get("exit_date") if var else None,
                "variant_exit_reason": var.get("exit_reason") if var else None,
                "variant_return_pct": var_ret,
                "force_exit_triggered": force_exit,
                "delta_return_pp": round(var_ret - b0_ret, 4) if var_ret is not None else None,
                "would_save_vs_b0": var_ret is not None and var_ret > b0_ret,
            }
        )
    return rows


def run_c18acc_rotation_force_exit_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-30",
    configs: list[ScoreSwapCConfig] | None = None,
) -> dict[str, Any]:
    from market_breadth_ma import build_breadth_panel
    from research.backtest.c18acc_intraday_1m_hold import _i36_live_config, apply_intraday_1m_hold

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    grid = configs or rotation_force_exit_sweep_configs()
    kbar_cache: dict = {}
    rows: list[dict[str, Any]] = []
    baseline_rotation: dict[str, Any] | None = None
    baseline_summary: dict[str, Any] | None = None
    b0_periods: list[dict[str, Any]] = []
    watch_by_variant: dict[str, list[dict[str, Any]]] = {}

    for cfg in grid:
        print(f"rotation force-exit sweep · {cfg.variant_id} · {cfg.label} ...", flush=True)
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
            "force_exits": summary.get("force_exits"),
            "n_periods": summary.get("n_periods"),
            "rotation_cohort": rot_stats,
            "config": cfg.to_dict(),
        }
        if cfg.variant_id == "B0":
            baseline_rotation = rot_stats
            baseline_summary = summary
            b0_periods = periods
            row["delta_vs_b0_mean_excess_pp"] = 0.0
            row["rotation_cohort_delta_mean_return_pp"] = 0.0
            watch_by_variant["B0"] = _watch_stock_counterfactual(b0_periods, periods, variant_id="B0")
        else:
            base_ex = (baseline_summary or {}).get("mean_excess_pct")
            ex = summary.get("mean_excess_pct")
            row["delta_vs_b0_mean_excess_pp"] = (
                round(float(ex) - float(base_ex), 4) if ex is not None and base_ex is not None else None
            )
            br = (baseline_rotation or {}).get("mean_return_pct")
            rr = rot_stats.get("mean_return_pct")
            row["rotation_cohort_delta_mean_return_pp"] = (
                round(float(rr) - float(br), 4) if rr is not None and br is not None else None
            )
            watch_by_variant[cfg.variant_id] = _watch_stock_counterfactual(
                b0_periods, periods, variant_id=cfg.variant_id
            )
        rows.append(row)
        print(
            f"  {cfg.variant_id}: excess={summary.get('mean_excess_pct')}% "
            f"swaps={summary.get('swaps_total')} force={summary.get('force_exits')} "
            f"rot_n={rot_stats.get('n')}",
            flush=True,
        )

    # B6 · B1 + I36 intraday overlay
    b1_cfg = next(c for c in grid if c.variant_id == "B1")
    print(f"rotation force-exit sweep · B6 · B1 + I36 overlay ...", flush=True)
    b1_periods, b1_summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=b1_cfg,
        kbar_cache=kbar_cache,
        rs_mom=rs_mom,
        rs_ratio=rs_ratio,
        entry_c_config=c0_cfg,
    )
    i36_cfg = _i36_live_config("I36", "combo_spike spike≥4% hybrid")
    i36_kbar_cache: dict = {}
    i36_periods, i36_summary = apply_intraday_1m_hold(
        conn,
        base_periods=b1_periods,
        close=close,
        full_dates=full_dates,
        config=i36_cfg,
        kbar_cache=i36_kbar_cache,
    )
    b6_rot = _rotation_cohort_periods(i36_periods, rs_ratio, rs_mom, full_dates)
    b6_rot_stats = _cohort_stats(b6_rot)
    base_ex = (baseline_summary or {}).get("mean_excess_pct")
    b6_ex = i36_summary.get("mean_excess_pct")
    b6_row = {
        "variant_id": "B6",
        "label": "B1 + I36 intraday overlay",
        "mean_excess_pct": b6_ex,
        "swaps_total": b1_summary.get("swaps_total"),
        "force_exits": b1_summary.get("force_exits"),
        "n_periods": i36_summary.get("n_periods"),
        "rotation_cohort": b6_rot_stats,
        "intraday_overlay": i36_summary,
        "delta_vs_b0_mean_excess_pp": (
            round(float(b6_ex) - float(base_ex), 4) if b6_ex is not None and base_ex is not None else None
        ),
        "rotation_cohort_delta_mean_return_pp": (
            round(float(b6_rot_stats["mean_return_pct"]) - float((baseline_rotation or {}).get("mean_return_pct")), 4)
            if b6_rot_stats.get("mean_return_pct") is not None
            and (baseline_rotation or {}).get("mean_return_pct") is not None
            else None
        ),
    }
    watch_by_variant["B6"] = _watch_stock_counterfactual(b0_periods, i36_periods, variant_id="B6")
    rows.append(b6_row)
    print(
        f"  B6: excess={b6_ex}% force={b1_summary.get('force_exits')} rot_n={b6_rot_stats.get('n')}",
        flush=True,
    )

    ranked = sorted(rows, key=lambda r: (-(r.get("mean_excess_pct") or -999.0), r.get("variant_id", "")))
    best = ranked[0] if ranked else None

    return {
        "topic": "c18acc-rotation-exit",
        "track": "B",
        "date_start": date_start,
        "date_end": date_end,
        "baseline_variant": "B0",
        "watch_stocks": list(WATCH_STOCKS),
        "summaries": rows,
        "best_by_mean_excess": best,
        "watch_stock_counterfactual": watch_by_variant,
        "hypotheses": {
            "H-ROT-B1": "1d lagging force exit 減 rotation cohort 大虧且不傷全樣本均超額",
            "H-ROT-B2": "2d weakening force exit 對 gradual rotation（4979/8996）有效",
            "H-ROT-B3": "1d weakening force exit 早於 lagging 出場 · save > B1",
            "H-ROT-B4": "weakening_or_lagging 1d 覆蓋面最大 · rotation save 最佳",
            "H-ROT-B5": "B2 + accel_sell_negative_only=False 組合優於單獨 B2",
            "H-ROT-B6": "B1 force exit + I36 盤中 overlay 保留 rotation save 且 Δ B0 ≥ −0.3pp",
        },
    }


def render_c18acc_rotation_force_exit_sweep_md(payload: dict[str, Any]) -> str:
    lines = [
        "# C18acc · Track B Rotation Force-Exit Sweep",
        "",
        f"區間：{payload.get('date_start')} .. {payload.get('date_end')}",
        "",
        "| id | label | mean_excess% | swaps | force_exits | n | rot_n | rot_mean_ret% | Δexcess vs B0 | Δrot_ret vs B0 |",
        "|----|-------|--------------|-------|-------------|---|-------|---------------|---------------|----------------|",
    ]
    for s in payload.get("summaries") or []:
        rot = s.get("rotation_cohort") or {}
        lines.append(
            f"| {s.get('variant_id')} | {s.get('label')} "
            f"| {s.get('mean_excess_pct')} | {s.get('swaps_total')} | {s.get('force_exits')} "
            f"| {s.get('n_periods')} | {rot.get('n')} | {rot.get('mean_return_pct')} "
            f"| {s.get('delta_vs_b0_mean_excess_pp')} | {s.get('rotation_cohort_delta_mean_return_pp')} |"
        )
    best = payload.get("best_by_mean_excess") or {}
    if best:
        lines += [
            "",
            "## Best by mean excess",
            "",
            f"- **{best.get('variant_id')}** · {best.get('label')}",
            f"- mean_excess={best.get('mean_excess_pct')}% · force_exits={best.get('force_exits')} "
            f"· Δ vs B0={best.get('delta_vs_b0_mean_excess_pp')}pp",
        ]
    watch = payload.get("watch_stock_counterfactual") or {}
    if watch:
        lines += [
            "",
            "## Watch stocks counterfactual (4979/8996/2467/6274/3293)",
            "",
            "| variant | stock | entry | b0_ret% | var_ret% | force_exit | Δpp | save? |",
            "|---------|-------|-------|---------|----------|------------|-----|-------|",
        ]
        for vid, legs in sorted(watch.items()):
            if vid == "B0":
                continue
            for leg in legs:
                lines.append(
                    f"| {vid} | {leg.get('stock_id')} | {leg.get('entry_date')} "
                    f"| {leg.get('b0_return_pct')} | {leg.get('variant_return_pct')} "
                    f"| {leg.get('force_exit_triggered')} | {leg.get('delta_return_pp')} "
                    f"| {leg.get('would_save_vs_b0')} |"
                )
    lines += [
        "",
        "---",
        "模組：`scripts/run_c18acc_rotation_force_exit_sweep.py`",
        "",
    ]
    return "\n".join(lines)
