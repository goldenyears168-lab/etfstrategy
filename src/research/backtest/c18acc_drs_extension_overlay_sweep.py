"""C18acc · drs / extension overlay research · slot re-sim vs S2 baseline.

Tracks:
  A · Exit overlay — drs_neg / spread_lost force-exit (replace or stack on S2)
  B · Relaxed pool — fresh_union_accel + optional struct exit
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal

import pandas as pd

from market_breadth_ma import build_breadth_panel
from market_benchmark import load_benchmark_close
from research.backtest.c18acc_rotation_force_exit_sweep import (
    _cohort_stats,
    _rotation_cohort_periods,
)
from research.backtest.c18acc_rotation_selective_exit_sweep import (
    _variant_from_base,
    rotation_selective_exit_sweep_configs,
)
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, LENGTH
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    champion_score_swap_c_config,
    close_champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods
from rrg_rotation import compute_rrg_panel
from rrg_universe_intraday_panel import WMA_LONG_LENGTH, WMA_SHORT_LENGTH

IS_END_DEFAULT = "2025-12-31"
OOS_START_DEFAULT = "2026-01-01"
OOS_H1_END_DEFAULT = "2026-06-30"
OOS_H2_START_DEFAULT = "2026-07-01"
OOS_H2_END_FORWARD_DEFAULT = None  # db latest
OOS_H2_HISTORICAL_START = "2025-07-01"
OOS_H2_HISTORICAL_END = "2025-12-31"
MIN_OOS_H2_TRADE_DATES_DEFAULT = 20
OosH2Mode = Literal["forward", "historical"]


def build_daily_spread_rs_panel(
    close: pd.DataFrame,
    bench: pd.Series,
) -> pd.DataFrame:
    """Daily close · WMA(5) − WMA(20) spread_rs."""
    rs5, _, _ = compute_rrg_panel(close, bench, length=WMA_SHORT_LENGTH)
    rs20, _, _ = compute_rrg_panel(close, bench, length=WMA_LONG_LENGTH)
    common = rs5.columns.intersection(rs20.columns)
    return rs5[common].subtract(rs20[common], axis=0)


def drs_extension_overlay_configs() -> list[ScoreSwapCConfig]:
    """Preregistered variants · baseline S2 + drs/extension exits + relaxed pool."""
    s2 = next(c for c in rotation_selective_exit_sweep_configs() if c.variant_id == "S2")
    champ = close_champion_score_swap_c_config()

    return [
        replace(s2, variant_id="S2", label="baseline · S2 quad weakening 2d + loss≥5%"),
        _variant_from_base(
            "DR1",
            "drs_neg 2d + loss≥5% · 取代 S2 quad",
            quad_force_exit_mode="none",
            struct_force_exit_mode="drs_neg",
            struct_force_exit_min_days=2,
            struct_force_exit_loss_pct=-0.05,
        ),
        _variant_from_base(
            "DR2",
            "spread_lost 1d + loss≥5% · 取代 S2 quad",
            quad_force_exit_mode="none",
            struct_force_exit_mode="spread_lost",
            struct_force_exit_min_days=1,
            struct_force_exit_loss_pct=-0.05,
        ),
        _variant_from_base(
            "DR3",
            "S2 + spread_lost 1d + loss≥3% · stack",
            struct_force_exit_mode="spread_lost",
            struct_force_exit_min_days=1,
            struct_force_exit_loss_pct=-0.03,
        ),
        _variant_from_base(
            "DR4",
            "S2 + drs_neg 2d + loss≥5% · stack",
            struct_force_exit_mode="drs_neg",
            struct_force_exit_min_days=2,
            struct_force_exit_loss_pct=-0.05,
        ),
        replace(
            champ,
            variant_id="POOL1",
            label="fresh∪accel pool · S2 exit",
            candidate_pool="fresh_union_accel",
            quad_force_exit_mode="weakening",
            quad_force_exit_min_days=2,
            quad_force_exit_loss_pct=-0.05,
        ),
        replace(
            champ,
            variant_id="POOL2",
            label="fresh∪accel · drs_neg exit",
            candidate_pool="fresh_union_accel",
            quad_force_exit_mode="none",
            struct_force_exit_mode="drs_neg",
            struct_force_exit_min_days=2,
            struct_force_exit_loss_pct=-0.05,
        ),
        replace(
            champ,
            variant_id="POOL3",
            label="entry fresh · swap mono_tier2 · S2",
            candidate_pool="fresh",
            swap_pool="mono_tier2",
            quad_force_exit_mode="weakening",
            quad_force_exit_min_days=2,
            quad_force_exit_loss_pct=-0.05,
        ),
    ]


def _calendar_years(date_start: str, date_end: str) -> float:
    d0 = datetime.strptime(date_start, "%Y-%m-%d")
    d1 = datetime.strptime(date_end, "%Y-%m-%d")
    return max((d1 - d0).days / 365.25, 1e-6)


def _run_variant_window(
    conn: sqlite3.Connection,
    *,
    cfg: ScoreSwapCConfig,
    date_start: str,
    date_end: str,
    label: str,
    close: pd.DataFrame,
    bench: pd.Series,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    spread_rs_panel: pd.DataFrame,
    full_dates: list[str],
    fresh_by_date: dict[str, list],
    mono_by_date: dict[str, list],
    zone_by_date: dict[str, str],
    c0_cfg: ScoreSwapCConfig,
    kbar_cache: dict,
    min_trade_dates: int = 5,
) -> dict[str, Any]:
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    if len(trade_dates) < min_trade_dates:
        return {
            "label": label,
            "error": "insufficient_trade_dates",
            "variant_id": cfg.variant_id,
            "n_trade_dates": len(trade_dates),
        }

    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
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
    leg_sum = summarize_periods(periods)
    port = portfolio_metrics_for_periods(
        conn,
        periods,
        trade_dates,
        total_capital=30_000.0,
        n_slots=cfg.n_slots,
        close=close,
    )
    rot = _cohort_stats(_rotation_cohort_periods(periods, rs_ratio, rs_mom, full_dates))
    struct_exits = sum(1 for p in periods if p.get("exit_reason") == "struct_force_exit")
    quad_exits = sum(1 for p in periods if p.get("exit_reason") == "quad_force_exit")

    return {
        "label": label,
        "variant_id": cfg.variant_id,
        "config_label": cfg.label,
        "date_start": date_start,
        "date_end": date_end,
        "n_trade_dates": len(trade_dates),
        "calendar_years": round(_calendar_years(date_start, date_end), 3),
        "n_periods": summary.get("n_periods"),
        "mean_excess_pct": summary.get("mean_excess_pct"),
        "mean_return_pct": leg_sum.get("mean_return_pct"),
        "win_rate_vs_bench_pct": leg_sum.get("win_rate_vs_bench_pct"),
        "swaps_total": summary.get("swaps_total"),
        "force_exits": summary.get("force_exits"),
        "mean_hold_days": summary.get("mean_hold_days"),
        "struct_force_exits": struct_exits,
        "quad_force_exits": quad_exits,
        "rotation_cohort": rot,
        "portfolio": {
            "total_return_pct": port.get("total_return_pct"),
            "cagr_pct": port.get("cagr_pct"),
            "sharpe_ratio": port.get("sharpe_ratio"),
            "max_drawdown_pct": port.get("max_drawdown_pct"),
        },
    }


def run_drs_extension_overlay_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-30",
    is_end: str = IS_END_DEFAULT,
    oos_start: str = OOS_START_DEFAULT,
) -> dict[str, Any]:
    configs = drs_extension_overlay_configs()
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
            rows.append(row)

    by_variant: dict[str, dict[str, Any]] = {}
    for r in rows:
        vid = str(r.get("variant_id", ""))
        by_variant.setdefault(vid, {})[str(r.get("label", ""))] = r

    s2_full = by_variant.get("S2", {}).get("FULL", {})
    s2_oos = by_variant.get("S2", {}).get("OOS", {})
    comparisons: list[dict[str, Any]] = []
    for cfg in configs:
        vid = cfg.variant_id
        if vid == "S2":
            continue
        full = by_variant.get(vid, {}).get("FULL", {})
        oos = by_variant.get(vid, {}).get("OOS", {})
        comparisons.append(
            {
                "variant_id": vid,
                "label": cfg.label,
                "delta_excess_vs_s2_full_pp": round(
                    float(full.get("mean_excess_pct") or 0)
                    - float(s2_full.get("mean_excess_pct") or 0),
                    3,
                ),
                "delta_excess_vs_s2_oos_pp": round(
                    float(oos.get("mean_excess_pct") or 0) - float(s2_oos.get("mean_excess_pct") or 0),
                    3,
                ),
                "delta_swaps_vs_s2_full": int(full.get("swaps_total") or 0)
                - int(s2_full.get("swaps_total") or 0),
                "delta_cagr_vs_s2_full_pp": round(
                    float((full.get("portfolio") or {}).get("cagr_pct") or 0)
                    - float((s2_full.get("portfolio") or {}).get("cagr_pct") or 0),
                    2,
                ),
                "full": full,
                "oos": oos,
            }
        )

    comparisons.sort(
        key=lambda x: (
            float(x.get("delta_excess_vs_s2_oos_pp") or 0),
            float(x.get("delta_swaps_vs_s2_full") or 0),
        ),
        reverse=True,
    )

    best_oos = comparisons[0] if comparisons else None
    beat_s2_oos = [
        c
        for c in comparisons
        if float(c.get("delta_excess_vs_s2_oos_pp") or 0) > 0
        and not (c.get("oos") or {}).get("error")
    ]

    return {
        "schema": "c18acc_drs_extension_overlay-v1",
        "topic": "c18acc-drs-extension-overlay",
        "baseline_variant": "S2",
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_start": oos_start,
        "hypotheses": {
            "H-DR-1": "drs_neg force-exit ≥ S2 on OOS mean excess",
            "H-DR-2": "spread_lost stack improves rotation cohort vs S2",
            "H-POOL-1": "fresh∪accel + struct exit raises swap count without excess collapse",
        },
        "variants": by_variant,
        "comparisons": comparisons,
        "verdict": {
            "best_oos_variant": best_oos.get("variant_id") if best_oos else None,
            "n_beat_s2_oos": len(beat_s2_oos),
            "beat_s2_oos_ids": [c["variant_id"] for c in beat_s2_oos],
            "s2_full_excess": s2_full.get("mean_excess_pct"),
            "s2_oos_excess": s2_oos.get("mean_excess_pct"),
            "summary": _verdict_summary(s2_full, s2_oos, beat_s2_oos, best_oos),
        },
    }


def _poll_champion_variant(variant_id: str, label: str, **overrides: Any) -> ScoreSwapCConfig:
    """C18acc champion · poll_5m · override fields."""
    base = champion_score_swap_c_config()
    fields = base.to_dict()
    fields["variant_id"] = variant_id
    fields["label"] = label
    fields.update(overrides)
    return ScoreSwapCConfig(**fields)


def pool1_poll5m_s2_configs() -> list[ScoreSwapCConfig]:
    """S2 vs POOL1 · live timing_mode=poll_5m (production-like)."""
    s2_exit = {
        "quad_force_exit_mode": "weakening",
        "quad_force_exit_min_days": 2,
        "quad_force_exit_loss_pct": -0.05,
    }
    return [
        _poll_champion_variant(
            "S2-P5M",
            "poll_5m · fresh · S2 exit",
            candidate_pool="fresh",
            **s2_exit,
        ),
        _poll_champion_variant(
            "POOL1-P5M",
            "poll_5m · fresh∪accel · S2 exit",
            candidate_pool="fresh_union_accel",
            **s2_exit,
        ),
    ]


def resolve_oos_h2_window(
    *,
    oos_h2_mode: OosH2Mode = "forward",
    oos_h2_start: str = OOS_H2_START_DEFAULT,
    oos_h2_end: str | None = OOS_H2_END_FORWARD_DEFAULT,
    date_end: str,
) -> tuple[str, str, OosH2Mode]:
    """Return (h2_start, h2_end, effective_mode) for OOS H2 evaluation."""
    if oos_h2_mode == "historical":
        return OOS_H2_HISTORICAL_START, OOS_H2_HISTORICAL_END, "historical"
    h2_end = date_end if oos_h2_end is None else min(oos_h2_end, date_end)
    return oos_h2_start, h2_end, "forward"


def run_pool1_poll5m_oos_holdout(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str | None = None,
    is_end: str = IS_END_DEFAULT,
    oos_h1_start: str = OOS_START_DEFAULT,
    oos_h1_end: str = OOS_H1_END_DEFAULT,
    oos_h2_mode: OosH2Mode = "forward",
    oos_h2_start: str = OOS_H2_START_DEFAULT,
    oos_h2_end: str | None = OOS_H2_END_FORWARD_DEFAULT,
    min_oos_h2_trade_dates: int = MIN_OOS_H2_TRADE_DATES_DEFAULT,
) -> dict[str, Any]:
    """POOL1 vs S2 · poll_5m slot re-sim · IS / OOS_H1 / OOS_H2 / FULL."""
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    if date_end is None:
        date_end = full_dates[-1]
    if date_end > full_dates[-1]:
        date_end = full_dates[-1]

    h2_start, h2_end, h2_mode = resolve_oos_h2_window(
        oos_h2_mode=oos_h2_mode,
        oos_h2_start=oos_h2_start,
        oos_h2_end=oos_h2_end,
        date_end=date_end,
    )
    h2_trade_dates = [d for d in full_dates if h2_start <= d <= h2_end]
    n_h2_td = len(h2_trade_dates)
    h2_ready = n_h2_td >= min_oos_h2_trade_dates
    h2_target_end = (
        h2_trade_dates[min_oos_h2_trade_dates - 1]
        if h2_ready
        else (h2_trade_dates[-1] if h2_trade_dates else None)
    )

    configs = pool1_poll5m_s2_configs()
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")

    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    spread_rs_panel = build_daily_spread_rs_panel(close, bench)
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    kbar_cache: dict = {}

    windows: list[tuple[str, str, str]] = [
        ("IS", date_start, is_end),
        ("OOS_H1", oos_h1_start, min(oos_h1_end, date_end)),
        ("FULL", date_start, date_end),
    ]
    if h2_ready and h2_start <= h2_end:
        windows.insert(2, ("OOS_H2", h2_start, h2_end))

    rows: list[dict[str, Any]] = []
    for cfg in configs:
        for win_label, w_start, w_end in windows:
            if w_start > w_end:
                continue
            min_td = 2 if win_label == "OOS_H2" else 5
            rows.append(
                _run_variant_window(
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
                    min_trade_dates=min_td,
                )
            )

    by_variant: dict[str, dict[str, Any]] = {}
    for r in rows:
        by_variant.setdefault(str(r.get("variant_id", "")), {})[str(r.get("label", ""))] = r

    s2 = by_variant.get("S2-P5M", {})
    p1 = by_variant.get("POOL1-P5M", {})
    s2_full = s2.get("FULL", {})
    s2_h1 = s2.get("OOS_H1", {})
    s2_h2 = s2.get("OOS_H2", {})
    p1_full = p1.get("FULL", {})
    p1_h1 = p1.get("OOS_H1", {})
    p1_h2 = p1.get("OOS_H2", {})

    def _delta(a: dict, b: dict, key: str) -> float | None:
        av, bv = a.get(key), b.get(key)
        if av is None or bv is None:
            return None
        return round(float(av) - float(bv), 4)

    comparison = {
        "delta_excess_full_pp": _delta(p1_full, s2_full, "mean_excess_pct"),
        "delta_excess_oos_h1_pp": _delta(p1_h1, s2_h1, "mean_excess_pct"),
        "delta_excess_oos_h2_pp": _delta(p1_h2, s2_h2, "mean_excess_pct"),
        "delta_swaps_full": int(p1_full.get("swaps_total") or 0) - int(s2_full.get("swaps_total") or 0),
        "delta_swaps_h1": int(p1_h1.get("swaps_total") or 0) - int(s2_h1.get("swaps_total") or 0),
        "delta_swaps_h2": int(p1_h2.get("swaps_total") or 0) - int(s2_h2.get("swaps_total") or 0),
        "delta_cagr_full_pp": round(
            float((p1_full.get("portfolio") or {}).get("cagr_pct") or 0)
            - float((s2_full.get("portfolio") or {}).get("cagr_pct") or 0),
            2,
        ),
    }

    beat_h1 = (comparison.get("delta_excess_oos_h1_pp") or 0) >= 0
    beat_h2 = (
        (comparison.get("delta_excess_oos_h2_pp") or 0) >= 0 if h2_ready and p1_h2 else None
    )

    h2_status = {
        "mode": h2_mode,
        "status": "ready" if h2_ready else "pending",
        "n_trade_dates": n_h2_td,
        "min_required": min_oos_h2_trade_dates,
        "window_start": h2_start,
        "window_end": h2_end,
        "db_latest": date_end,
        "target_end_date": h2_target_end,
        "note": (
            f"OOS H2 evaluable · {n_h2_td} TD ≥ {min_oos_h2_trade_dates} · {h2_start}..{h2_end}"
            if h2_ready
            else (
                f"pending · {n_h2_td}/{min_oos_h2_trade_dates} TD "
                f"({h2_start}..{h2_end}) · use --oos-h2-mode historical or rerun after daily_sync"
            )
        ),
    }

    return {
        "schema": "c18acc_pool1_poll5m_oos-v1",
        "topic": "c18acc-pool1-poll5m-oos",
        "timing_mode": "poll_5m",
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_h1": f"{oos_h1_start}..{min(oos_h1_end, date_end)}",
        "oos_h2": f"{h2_start}..{h2_end}" if h2_start <= h2_end else None,
        "oos_h2_mode": h2_mode,
        "oos_h2_gate": h2_status,
        "variants": by_variant,
        "pool1_vs_s2": comparison,
        "verdict": {
            "pool1_beats_s2_h1_excess": beat_h1,
            "pool1_beats_s2_h2_excess": beat_h2,
            "oos_h2_evaluated": h2_ready,
            "summary": (
                f"S2-P5M FULL excess={s2_full.get('mean_excess_pct')}% swaps={s2_full.get('swaps_total')} · "
                f"POOL1-P5M FULL excess={p1_full.get('mean_excess_pct')}% swaps={p1_full.get('swaps_total')} · "
                f"ΔH1={comparison.get('delta_excess_oos_h1_pp')}pp · "
                f"ΔH2={comparison.get('delta_excess_oos_h2_pp') if h2_ready else 'pending'} · "
                f"Δswaps FULL={comparison.get('delta_swaps_full')} · "
                f"H2 {h2_status['note']}"
            ),
        },
    }


def render_pool1_poll5m_oos_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict", {})
    cmp_ = payload.get("pool1_vs_s2", {})
    h2g = payload.get("oos_h2_gate", {})
    lines = [
        "# C18acc POOL1 vs S2 · poll_5m OOS holdout",
        "",
        "> Production-like timing · fresh∪accel vs fresh · S2 exit unchanged",
        "",
        f"- Span: **{payload.get('date_start')} → {payload.get('date_end')}**",
        f"- timing: **{payload.get('timing_mode')}**",
        f"- IS ≤ {payload.get('is_end')} · OOS H1: {payload.get('oos_h1')}",
        f"- OOS H2 ({h2g.get('mode', 'forward')}): {payload.get('oos_h2')} · gate: **{h2g.get('status', '—')}** "
        f"({h2g.get('n_trade_dates', 0)}/{h2g.get('min_required', 20)} TD)",
        "",
        "## Verdict",
        "",
        f"- {v.get('summary', '')}",
        "",
    ]
    if h2g.get("mode") == "historical":
        lines.extend(
            [
                "> OOS H2 **historical proxy** — 2025 H2 retrospective · same-period POOL1 vs S2 A/B "
                "(not forward 2026 H2 holdout).",
                "",
            ]
        )
    elif h2g.get("status") == "pending":
        lines.extend(
            [
                "> OOS H2 **pending** — need ≥20 trading days after 2026-07-01. "
                "Use `--oos-h2-mode historical` for 2025 H2 backtest, or rerun after `daily_sync`.",
                "",
            ]
        )
    lines.extend(
        [
            "## POOL1 vs S2",
            "",
            "| window | Δ excess (pp) | Δ swaps |",
            "|--------|---------------|---------|",
            f"| FULL | {cmp_.get('delta_excess_full_pp', '—')} | {cmp_.get('delta_swaps_full', '—')} |",
            f"| OOS H1 | {cmp_.get('delta_excess_oos_h1_pp', '—')} | {cmp_.get('delta_swaps_h1', '—')} |",
            f"| OOS H2 | {cmp_.get('delta_excess_oos_h2_pp', '—' if v.get('oos_h2_evaluated') else 'pending')} | "
            f"{cmp_.get('delta_swaps_h2', '—' if v.get('oos_h2_evaluated') else 'pending')} |",
            f"| Δ CAGR FULL | {cmp_.get('delta_cagr_full_pp', '—')}pp | |",
            "",
            "## By variant",
            "",
        ]
    )
    for vid in ("S2-P5M", "POOL1-P5M"):
        block = payload.get("variants", {}).get(vid, {})
        lines.append(f"### {vid}")
        for win in ("FULL", "IS", "OOS_H1", "OOS_H2"):
            b = block.get(win)
            if not b or b.get("error"):
                continue
            port = b.get("portfolio") or {}
            lines.append(
                f"- **{win}**: excess={b.get('mean_excess_pct')}% · swaps={b.get('swaps_total')} · "
                f"force={b.get('force_exits')} · n_dates={b.get('n_trade_dates')} · CAGR={port.get('cagr_pct')}%"
            )
        lines.append("")
    return "\n".join(lines)


def _verdict_summary(
    s2_full: dict[str, Any],
    s2_oos: dict[str, Any],
    beat_s2_oos: list[dict[str, Any]],
    best_oos: dict[str, Any] | None,
) -> str:
    parts = [
        f"S2 FULL excess={s2_full.get('mean_excess_pct')}% · OOS={s2_oos.get('mean_excess_pct')}%",
        f"swaps={s2_full.get('swaps_total')}",
    ]
    if beat_s2_oos:
        parts.append(f"{len(beat_s2_oos)} variant(s) beat S2 OOS excess")
    else:
        parts.append("no variant beat S2 OOS excess")
    if best_oos:
        parts.append(
            f"best OOS={best_oos.get('variant_id')} "
            f"Δ={best_oos.get('delta_excess_vs_s2_oos_pp')}pp "
            f"Δswaps={best_oos.get('delta_swaps_vs_s2_full')}"
        )
    return " · ".join(parts)


def render_drs_extension_overlay_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict", {})
    lines = [
        "# C18acc · drs / extension overlay sweep",
        "",
        "> drs_neg / spread_lost force-exit · vs S2 · relaxed pool arms",
        "",
        f"- Span: **{payload.get('date_start')} → {payload.get('date_end')}**",
        f"- IS ≤ {payload.get('is_end')} · OOS ≥ {payload.get('oos_start')}",
        f"- Baseline: **{payload.get('baseline_variant')}**",
        "",
        "## Verdict",
        "",
        f"- {v.get('summary', '')}",
        "",
        "## vs S2 (FULL / OOS)",
        "",
        "| variant | Δ excess FULL | Δ excess OOS | Δ swaps | Δ CAGR | struct exits |",
        "|---------|---------------|--------------|---------|--------|--------------|",
    ]
    for c in payload.get("comparisons", []):
        full = c.get("full") or {}
        lines.append(
            f"| {c.get('variant_id')} | {c.get('delta_excess_vs_s2_full_pp'):+.3f}pp | "
            f"{c.get('delta_excess_vs_s2_oos_pp'):+.3f}pp | {c.get('delta_swaps_vs_s2_full'):+d} | "
            f"{c.get('delta_cagr_vs_s2_full_pp'):+.2f}pp | {full.get('struct_force_exits', 0)} |"
        )

    lines.extend(["", "## S2 reference", ""])
    s2 = (payload.get("variants") or {}).get("S2", {})
    for win in ("FULL", "OOS", "IS"):
        block = s2.get(win, {})
        if not block:
            continue
        port = block.get("portfolio") or {}
        lines.append(
            f"- **{win}**: excess={block.get('mean_excess_pct')}% · "
            f"swaps={block.get('swaps_total')} · CAGR={port.get('cagr_pct')}% · "
            f"force={block.get('force_exits')}"
        )

    lines.extend(
        [
            "",
            "## Hypotheses",
            "",
        ]
    )
    for hid, stmt in (payload.get("hypotheses") or {}).items():
        lines.append(f"- **{hid}**: {stmt}")

    return "\n".join(lines)
