"""C18acc · POOL1 + S2 · hard stop / S2 loss threshold research sweep."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any

import pandas as pd

from market_breadth_ma import build_breadth_panel
from market_benchmark import load_benchmark_close
from research.backtest.c18acc_drs_extension_overlay_sweep import (
    IS_END_DEFAULT,
    OOS_START_DEFAULT,
    _run_variant_window,
    build_daily_spread_rs_panel,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, LENGTH
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_rotation import compute_rrg_panel


def _pool1_s2_base() -> ScoreSwapCConfig:
    base = champion_score_swap_c_config()
    return replace(
        base,
        variant_id="S2-POOL1",
        label="POOL1 · S2 weakening 2d + loss≥5%",
        quad_force_exit_mode="weakening",
        quad_force_exit_min_days=2,
        quad_force_exit_loss_pct=-0.05,
        quad_force_exit_requires_min_hold=True,
        hard_stop_loss_pct=None,
        hard_stop_intraday=False,
    )


def hard_stop_sweep_configs() -> list[ScoreSwapCConfig]:
    """Close hard stop · intraday hard stop · S2 loss threshold variants."""
    s2 = _pool1_s2_base()
    return [
        s2,
        replace(
            s2,
            variant_id="S2L8",
            label="POOL1 · S2 weakening 2d + loss≥8% (no hard stop)",
            hard_stop_loss_pct=None,
            quad_force_exit_loss_pct=-0.08,
        ),
        replace(
            s2,
            variant_id="HS8",
            label="POOL1 · S2 + close hard stop −8%",
            hard_stop_loss_pct=-0.08,
        ),
        replace(
            s2,
            variant_id="HS10",
            label="POOL1 · S2 + close hard stop −10%",
            hard_stop_loss_pct=-0.10,
        ),
        replace(
            s2,
            variant_id="IHS8",
            label="POOL1 · S2 + intraday 1m hard stop −8%",
            hard_stop_loss_pct=-0.08,
            hard_stop_intraday=True,
        ),
        replace(
            s2,
            variant_id="IHS10",
            label="POOL1 · S2 + intraday 1m hard stop −10%",
            hard_stop_loss_pct=-0.10,
            hard_stop_intraday=True,
        ),
        replace(
            s2,
            variant_id="PLC10",
            label="POOL1 · S2 + portfolio total loss cap 10%",
            portfolio_loss_cap_pct=0.10,
        ),
    ]


def _legs_worse_than(periods: list[dict[str, Any]], threshold_pct: float) -> dict[str, Any]:
    bad = [p for p in periods if (p.get("return_pct") or 0) <= threshold_pct]
    return {
        "count": len(bad),
        "pct_of_legs": round(100.0 * len(bad) / max(len(periods), 1), 2),
        "worst_return_pct": min((p.get("return_pct") or 0) for p in periods) if periods else None,
    }


def run_c18acc_hard_stop_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-07-03",
    is_end: str = IS_END_DEFAULT,
    oos_start: str = OOS_START_DEFAULT,
) -> dict[str, Any]:
    configs = hard_stop_sweep_configs()
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    spread_rs_panel = build_daily_spread_rs_panel(close, bench)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    kbar_cache: dict = {}

    windows = [
        ("IS", date_start, is_end),
        ("OOS", oos_start, date_end),
        ("FULL", date_start, date_end),
    ]

    rows: list[dict[str, Any]] = []
    baseline_by_window: dict[str, dict[str, Any]] = {}

    for cfg in configs:
        for win_label, w_start, w_end in windows:
            if w_start > w_end:
                continue
            row = _run_variant_window(
                conn,
                cfg=cfg,
                date_start=w_start,
                date_end=w_end,
                label=win_label,
                close=close,
                bench=bench,
                rs_ratio=rs_ratio,
                rs_mom=rs_mom,
                spread_rs_panel=spread_rs_panel,
                full_dates=full_dates,
                fresh_by_date=fresh_by_date,
                mono_by_date=mono_by_date,
                zone_by_date=zone_by_date,
                c0_cfg=c0_cfg,
                kbar_cache=kbar_cache,
            )
            if row.get("error"):
                rows.append(row)
                continue

            w_trade_dates = [d for d in full_dates if w_start <= d <= w_end]
            periods, _ = simulate_score_swap_c(
                conn,
                trade_dates=w_trade_dates,
                full_dates=full_dates,
                close=close,
                bench=bench,
                fresh_by_date=fresh_by_date,
                zone_by_date=zone_by_date,
                config=cfg,
                mono_by_date=mono_by_date,
                rs_ratio=rs_ratio,
                rs_mom=rs_mom,
                spread_rs_panel=spread_rs_panel,
                kbar_cache=kbar_cache,
                entry_c_config=c0_cfg,
            )
            hard_stops = sum(
                1 for p in periods if p.get("exit_reason") in ("hard_stop", "hard_stop_intraday")
            )
            portfolio_cap_exits = sum(1 for p in periods if p.get("exit_reason") == "portfolio_loss_cap")
            intraday_stops = sum(1 for p in periods if p.get("exit_reason") == "hard_stop_intraday")
            row["hard_stop_exits"] = hard_stops
            row["portfolio_loss_cap_exits"] = portfolio_cap_exits
            row["hard_stop_intraday_exits"] = intraday_stops
            row["legs_lte_minus_10pct"] = _legs_worse_than(periods, -10.0)
            row["legs_lte_minus_8pct"] = _legs_worse_than(periods, -8.0)

            if cfg.variant_id == "S2-POOL1":
                baseline_by_window[win_label] = row
            base = baseline_by_window.get(win_label)
            if base and cfg.variant_id != "S2-POOL1":
                row["delta_mean_excess_pp"] = round(
                    float(row.get("mean_excess_pct") or 0) - float(base.get("mean_excess_pct") or 0),
                    4,
                )
                row["delta_swaps"] = int(row.get("swaps_total") or 0) - int(base.get("swaps_total") or 0)
                row["delta_cagr_pp"] = round(
                    float((row.get("portfolio") or {}).get("cagr_pct") or 0)
                    - float((base.get("portfolio") or {}).get("cagr_pct") or 0),
                    4,
                )
                row["delta_total_return_pp"] = round(
                    float((row.get("portfolio") or {}).get("total_return_pct") or 0)
                    - float((base.get("portfolio") or {}).get("total_return_pct") or 0),
                    4,
                )
            rows.append(row)

    return {
        "schema": "c18acc_hard_stop_sweep-v2",
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_start": oos_start,
        "baseline_variant": "S2-POOL1",
        "variants": [c.variant_id for c in configs],
        "rows": rows,
    }


def render_c18acc_hard_stop_sweep_md(payload: dict[str, Any]) -> str:
    lines = [
        "# C18acc · POOL1 + S2 · hard stop / S2 loss threshold sweep",
        "",
        f"- Span: **{payload.get('date_start')} → {payload.get('date_end')}**",
        f"- IS ≤ {payload.get('is_end')} · OOS ≥ {payload.get('oos_start')}",
        f"- Baseline: **{payload.get('baseline_variant')}**",
        "",
        "## vs S2-POOL1 (FULL)",
        "",
        "| variant | Δ excess | Δ OOS | Δ swaps | hard stop | intraday HS | port cap exit | legs≤−10% | worst leg |",
        "|---------|----------|-------|---------|-----------|-------------|---------------|-----------|-----------|",
    ]

    for vid in payload.get("variants") or []:
        if vid == "S2-POOL1":
            continue
        full = next((r for r in payload["rows"] if r.get("variant_id") == vid and r.get("label") == "FULL"), None)
        oos = next((r for r in payload["rows"] if r.get("variant_id") == vid and r.get("label") == "OOS"), None)
        if not full:
            continue
        legs10 = (full.get("legs_lte_minus_10pct") or {}).get("count", "—")
        worst = (full.get("legs_lte_minus_10pct") or {}).get("worst_return_pct")
        worst_s = f"{worst:.2f}%" if worst is not None else "—"
        lines.append(
            f"| {vid} | {full.get('delta_mean_excess_pp', '—')} pp | "
            f"{oos.get('delta_mean_excess_pp', '—') if oos else '—'} pp | "
            f"{full.get('delta_swaps', '—')} | {full.get('hard_stop_exits', '—')} | "
            f"{full.get('hard_stop_intraday_exits', '—')} | {full.get('portfolio_loss_cap_exits', '—')} | "
            f"{legs10} | {worst_s} |"
        )

    lines.extend(
        [
            "",
            "## FULL detail",
            "",
            "| variant | mean excess | total ret | swaps | quad FE | hard stop | legs≤−10% | legs≤−8% |",
            "|---------|-------------|-----------|-------|---------|-----------|-----------|----------|",
        ]
    )
    for r in payload.get("rows") or []:
        if r.get("label") != "FULL" or r.get("error"):
            continue
        port = r.get("portfolio") or {}
        legs10 = (r.get("legs_lte_minus_10pct") or {}).get("count", "—")
        legs8 = (r.get("legs_lte_minus_8pct") or {}).get("count", "—")
        lines.append(
            f"| {r.get('variant_id')} | {r.get('mean_excess_pct')} | "
            f"{port.get('total_return_pct')} | {r.get('swaps_total')} | "
            f"{r.get('quad_force_exits')} | {r.get('hard_stop_exits')} | {legs10} | {legs8} |"
        )
    return "\n".join(lines) + "\n"
