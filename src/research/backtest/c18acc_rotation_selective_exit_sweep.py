"""C18acc rotation-exit · Track B2 selective force-exit (quad + unrealized loss gate)."""

from __future__ import annotations

import sqlite3
from typing import Any

import pandas as pd

from market_benchmark import load_benchmark_close
from research.backtest.c18acc_rotation_force_exit_sweep import (
    _cohort_stats,
    _leg_key,
    _rotation_cohort_periods,
    _watch_stock_counterfactual,
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


def rotation_selective_exit_sweep_configs() -> list[ScoreSwapCConfig]:
    """Track B2 · S0–S7 selective force-exit variants (S6 = best S1–S5 + I36 at runtime)."""
    return [
        _variant_from_base("S0", "baseline · champion close (no force exit)"),
        _variant_from_base(
            "S1",
            "selective · 2d weakening + loss≥3%",
            quad_force_exit_mode="weakening",
            quad_force_exit_min_days=2,
            quad_force_exit_loss_pct=-0.03,
        ),
        _variant_from_base(
            "S2",
            "selective · 2d weakening + loss≥5%",
            quad_force_exit_mode="weakening",
            quad_force_exit_min_days=2,
            quad_force_exit_loss_pct=-0.05,
        ),
        _variant_from_base(
            "S3",
            "selective · 1d weakening + loss≥3%",
            quad_force_exit_mode="weakening",
            quad_force_exit_min_days=1,
            quad_force_exit_loss_pct=-0.03,
        ),
        _variant_from_base(
            "S4",
            "selective · 1d weakening + loss≥5%",
            quad_force_exit_mode="weakening",
            quad_force_exit_min_days=1,
            quad_force_exit_loss_pct=-0.05,
        ),
        _variant_from_base(
            "S5",
            "selective · weakening_or_lagging 1d + loss≥3%",
            quad_force_exit_mode="weakening_or_lagging",
            quad_force_exit_min_days=1,
            quad_force_exit_loss_pct=-0.03,
        ),
        _variant_from_base(
            "S7",
            "selective · 1d lagging + loss≥3%",
            quad_force_exit_mode="lagging",
            quad_force_exit_min_days=1,
            quad_force_exit_loss_pct=-0.03,
        ),
    ]


def _pick_best_selective(rows: list[dict[str, Any]], *, baseline_variant: str = "S0") -> dict[str, Any] | None:
    """Best S1–S5 by rotation cohort Δ mean return vs baseline; tiebreak mean_excess."""
    candidates = [
        r
        for r in rows
        if str(r.get("variant_id", "")).startswith("S")
        and r.get("variant_id") not in {baseline_variant, "S6", "S7"}
        and r.get("variant_id") != "S0"
    ]
    if not candidates:
        return None

    def _key(r: dict[str, Any]) -> tuple[float, float, str]:
        rot_delta = r.get("rotation_cohort_delta_mean_return_pp")
        excess = r.get("mean_excess_pct")
        return (
            float(rot_delta) if rot_delta is not None else -999.0,
            float(excess) if excess is not None else -999.0,
            str(r.get("variant_id", "")),
        )

    return max(candidates, key=_key)


def run_c18acc_rotation_selective_exit_sweep(
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
    grid = configs or rotation_selective_exit_sweep_configs()
    kbar_cache: dict = {}
    rows: list[dict[str, Any]] = []
    baseline_rotation: dict[str, Any] | None = None
    baseline_summary: dict[str, Any] | None = None
    s0_periods: list[dict[str, Any]] = []
    watch_by_variant: dict[str, list[dict[str, Any]]] = {}
    cfg_by_id: dict[str, ScoreSwapCConfig] = {}

    for cfg in grid:
        print(f"rotation selective-exit sweep · {cfg.variant_id} · {cfg.label} ...", flush=True)
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
        cfg_by_id[cfg.variant_id] = cfg
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
        if cfg.variant_id == "S0":
            baseline_rotation = rot_stats
            baseline_summary = summary
            s0_periods = periods
            row["delta_vs_s0_mean_excess_pp"] = 0.0
            row["rotation_cohort_delta_mean_return_pp"] = 0.0
            watch_by_variant["S0"] = _watch_stock_counterfactual(s0_periods, periods, variant_id="S0")
        else:
            base_ex = (baseline_summary or {}).get("mean_excess_pct")
            ex = summary.get("mean_excess_pct")
            row["delta_vs_s0_mean_excess_pp"] = (
                round(float(ex) - float(base_ex), 4) if ex is not None and base_ex is not None else None
            )
            br = (baseline_rotation or {}).get("mean_return_pct")
            rr = rot_stats.get("mean_return_pct")
            row["rotation_cohort_delta_mean_return_pp"] = (
                round(float(rr) - float(br), 4) if rr is not None and br is not None else None
            )
            watch_by_variant[cfg.variant_id] = _watch_stock_counterfactual(
                s0_periods, periods, variant_id=cfg.variant_id
            )
        rows.append(row)
        print(
            f"  {cfg.variant_id}: excess={summary.get('mean_excess_pct')}% "
            f"swaps={summary.get('swaps_total')} force={summary.get('force_exits')} "
            f"rot_n={rot_stats.get('n')}",
            flush=True,
        )

    best_selective = _pick_best_selective(rows)
    best_id = str((best_selective or {}).get("variant_id") or "S1")
    best_cfg = cfg_by_id.get(best_id) or next(c for c in grid if c.variant_id == "S1")
    print(
        f"rotation selective-exit sweep · S6 · {best_id} + I36 overlay ...",
        flush=True,
    )
    s6_base_periods, s6_base_summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=best_cfg,
        kbar_cache=kbar_cache,
        rs_mom=rs_mom,
        rs_ratio=rs_ratio,
        entry_c_config=c0_cfg,
    )
    i36_cfg = _i36_live_config("I36", "combo_spike spike≥4% hybrid")
    i36_kbar_cache: dict = {}
    s6_periods, s6_summary = apply_intraday_1m_hold(
        conn,
        base_periods=s6_base_periods,
        close=close,
        full_dates=full_dates,
        config=i36_cfg,
        kbar_cache=i36_kbar_cache,
    )
    s6_rot = _rotation_cohort_periods(s6_periods, rs_ratio, rs_mom, full_dates)
    s6_rot_stats = _cohort_stats(s6_rot)
    base_ex = (baseline_summary or {}).get("mean_excess_pct")
    s6_ex = s6_summary.get("mean_excess_pct")
    s6_row = {
        "variant_id": "S6",
        "label": f"{best_id} + I36 intraday overlay",
        "mean_excess_pct": s6_ex,
        "swaps_total": s6_base_summary.get("swaps_total"),
        "force_exits": s6_base_summary.get("force_exits"),
        "n_periods": s6_summary.get("n_periods"),
        "rotation_cohort": s6_rot_stats,
        "intraday_overlay": s6_summary,
        "selective_base_variant": best_id,
        "delta_vs_s0_mean_excess_pp": (
            round(float(s6_ex) - float(base_ex), 4) if s6_ex is not None and base_ex is not None else None
        ),
        "rotation_cohort_delta_mean_return_pp": (
            round(float(s6_rot_stats["mean_return_pct"]) - float((baseline_rotation or {}).get("mean_return_pct")), 4)
            if s6_rot_stats.get("mean_return_pct") is not None
            and (baseline_rotation or {}).get("mean_return_pct") is not None
            else None
        ),
    }
    watch_by_variant["S6"] = _watch_stock_counterfactual(s0_periods, s6_periods, variant_id="S6")
    rows.append(s6_row)
    print(
        f"  S6: excess={s6_ex}% force={s6_base_summary.get('force_exits')} rot_n={s6_rot_stats.get('n')}",
        flush=True,
    )

    ranked = sorted(rows, key=lambda r: (-(r.get("mean_excess_pct") or -999.0), r.get("variant_id", "")))
    best = ranked[0] if ranked else None

    return {
        "topic": "c18acc-rotation-exit",
        "track": "B2",
        "date_start": date_start,
        "date_end": date_end,
        "baseline_variant": "S0",
        "watch_stocks": ["4979", "8996", "2467", "6274", "3293"],
        "summaries": rows,
        "best_by_mean_excess": best,
        "best_selective_s1_s5": best_selective,
        "watch_stock_counterfactual": watch_by_variant,
        "hypotheses": {
            "H-ROT-B2-S1": "2d weakening + loss≥3% 減 rotation 大虧且全樣本 Δ S0 ≥ −0.2pp",
            "H-ROT-B2-S3": "1d weakening + loss≥3% 早於 S1 出場 · rotation save > S1",
            "H-ROT-B2-S5": "weakening_or_lagging 1d + loss≥3% 覆蓋 gradual rotation",
            "H-ROT-B2-S6": "best selective S1–S5 + I36 保留 rotation save 且 Δ S0 ≥ −0.3pp",
            "H-ROT-B2-S7": "1d lagging + loss≥3% 少於 B1 誤觸 · rotation save 接近 B1",
        },
    }


def render_c18acc_rotation_selective_exit_sweep_md(payload: dict[str, Any]) -> str:
    lines = [
        "# C18acc · Track B2 Rotation Selective Force-Exit Sweep",
        "",
        f"區間：{payload.get('date_start')} .. {payload.get('date_end')}",
        "",
        "| id | label | mean_excess% | force_exits | swaps | rot_n | rot_mean_ret% | Δexcess vs S0 | Δrot_ret vs S0 |",
        "|----|-------|--------------|-------------|-------|-------|---------------|---------------|----------------|",
    ]
    for s in payload.get("summaries") or []:
        rot = s.get("rotation_cohort") or {}
        lines.append(
            f"| {s.get('variant_id')} | {s.get('label')} "
            f"| {s.get('mean_excess_pct')} | {s.get('force_exits')} | {s.get('swaps_total')} "
            f"| {rot.get('n')} | {rot.get('mean_return_pct')} "
            f"| {s.get('delta_vs_s0_mean_excess_pp')} | {s.get('rotation_cohort_delta_mean_return_pp')} |"
        )
    best = payload.get("best_by_mean_excess") or {}
    if best:
        lines += [
            "",
            "## Best by mean excess",
            "",
            f"- **{best.get('variant_id')}** · {best.get('label')}",
            f"- mean_excess={best.get('mean_excess_pct')}% · force_exits={best.get('force_exits')} "
            f"· Δ vs S0={best.get('delta_vs_s0_mean_excess_pp')}pp",
        ]
    best_sel = payload.get("best_selective_s1_s5") or {}
    if best_sel:
        lines += [
            "",
            "## Best selective (S1–S5) by rotation cohort Δ",
            "",
            f"- **{best_sel.get('variant_id')}** · rot_Δ={best_sel.get('rotation_cohort_delta_mean_return_pp')}pp "
            f"· Δ excess={best_sel.get('delta_vs_s0_mean_excess_pp')}pp",
        ]
    watch = payload.get("watch_stock_counterfactual") or {}
    if watch:
        lines += [
            "",
            "## Watch stocks counterfactual (4979/8996/2467/6274/3293)",
            "",
            "| variant | stock | entry | s0_ret% | var_ret% | force_exit | Δpp | save? |",
            "|---------|-------|-------|---------|----------|------------|-----|-------|",
        ]
        for vid, legs in sorted(watch.items()):
            if vid == "S0":
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
        "模組：`scripts/run_c18acc_rotation_selective_exit_sweep.py`",
        "",
    ]
    return "\n".join(lines)
