"""C18acc · POOL1 seg_last bottom-tercile gate · poll_5m slot re-sim.

Gate (PIT): each signal day, within fresh∪accel candidate pool, exclude stocks
with seg_last in the cross-sectional bottom 1/3 (keep top 2/3).
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal

import pandas as pd

from market_breadth_ma import build_breadth_panel
from market_benchmark import load_benchmark_close
from research.backtest.c18acc_drs_extension_overlay_sweep import (
    MIN_OOS_H2_TRADE_DATES_DEFAULT,
    OOS_H1_END_DEFAULT,
    OOS_H2_END_FORWARD_DEFAULT,
    OOS_H2_HISTORICAL_END,
    OOS_H2_HISTORICAL_START,
    OOS_H2_START_DEFAULT,
    OOS_START_DEFAULT,
    IS_END_DEFAULT,
    OosH2Mode,
    _poll_champion_variant,
    build_daily_spread_rs_panel,
    resolve_oos_h2_window,
)
from research.backtest.c18acc_rotation_force_exit_sweep import (
    _cohort_stats,
    _rotation_cohort_periods,
)
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, LENGTH
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    _fresh_union_accel_pool,
    simulate_score_swap_c,
)
from rrg_mono_daily_brief import ScanRow
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods
from rrg_rotation import compute_rrg_panel

SegLastGateMode = Literal["exclude_bottom_tercile", "top_two_thirds"]


def build_seg_last_pass_gate_lookup(
    trade_dates: list[str],
    fresh_by_date: dict[str, list[ScanRow]],
    mono_by_date: dict[str, list[ScanRow]],
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    *,
    exclude_fraction: float = 1.0 / 3.0,
    pool: Literal["fresh_union_accel", "fresh"] = "fresh_union_accel",
    accel_lookback: int = 4,
) -> dict[str, set[str]]:
    """Per-day whitelist: candidates passing seg_last gate (exclude bottom tercile)."""
    out: dict[str, set[str]] = {}
    for as_of in trade_dates:
        fresh_mono = fresh_by_date.get(as_of, [])
        if pool == "fresh":
            pool_rows = list(fresh_mono)
        else:
            mono_rows = mono_by_date.get(as_of, [])
            pool_rows = _fresh_union_accel_pool(
                fresh_mono,
                mono_rows,
                rs_ratio,
                rs_mom,
                full_dates,
                as_of,
                lb=accel_lookback,
            )
        if not pool_rows:
            out[as_of] = set()
            continue
        segs = sorted(float(r.seg_last) for r in pool_rows)
        cut_idx = max(0, min(len(segs) - 1, int(len(segs) * exclude_fraction) - 1))
        cutoff = segs[cut_idx] if segs else 0.0
        passed = {r.stock_id for r in pool_rows if float(r.seg_last) > cutoff}
        # Tie at cutoff: include ties above strict > to avoid empty days on small pools
        if not passed:
            passed = {r.stock_id for r in pool_rows if float(r.seg_last) >= cutoff}
        out[as_of] = passed
    return out


def gate_coverage_stats(
    gate_lookup: dict[str, set[str]],
    trade_dates: list[str],
    fresh_by_date: dict[str, list[ScanRow]],
    mono_by_date: dict[str, list[ScanRow]],
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
) -> dict[str, Any]:
    sizes_pool: list[int] = []
    sizes_pass: list[int] = []
    excluded_counts: list[int] = []
    for as_of in trade_dates:
        fresh_mono = fresh_by_date.get(as_of, [])
        pool_rows = _fresh_union_accel_pool(
            fresh_mono,
            mono_by_date.get(as_of, []),
            rs_ratio,
            rs_mom,
            full_dates,
            as_of,
        )
        n_pool = len(pool_rows)
        n_pass = len(gate_lookup.get(as_of, set()))
        sizes_pool.append(n_pool)
        sizes_pass.append(n_pass)
        excluded_counts.append(max(0, n_pool - n_pass))
    n = len(trade_dates) or 1
    return {
        "mean_pool_size": round(sum(sizes_pool) / n, 2),
        "mean_pass_size": round(sum(sizes_pass) / n, 2),
        "mean_excluded": round(sum(excluded_counts) / n, 2),
        "days_empty_after_gate": sum(1 for s in sizes_pass if s == 0),
    }


def pool1_seg_last_gate_configs() -> list[ScoreSwapCConfig]:
    s2_exit = {
        "quad_force_exit_mode": "weakening",
        "quad_force_exit_min_days": 2,
        "quad_force_exit_loss_pct": -0.05,
    }
    return [
        _poll_champion_variant(
            "POOL1-P5M",
            "poll_5m · fresh∪accel · S2 exit",
            candidate_pool="fresh_union_accel",
            **s2_exit,
        ),
        _poll_champion_variant(
            "POOL1-P5M-SEG",
            "poll_5m · fresh∪accel · seg_last 排除低1/3 · S2 exit",
            candidate_pool="fresh_union_accel",
            **s2_exit,
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
    fresh_by_date: dict[str, list[ScanRow]],
    mono_by_date: dict[str, list[ScanRow]],
    zone_by_date: dict[str, str],
    c0_cfg: ScoreSwapCConfig,
    kbar_cache: dict,
    entry_gate_by_date: dict[str, set[str]] | None = None,
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
        entry_gate_by_date=entry_gate_by_date,
        swap_gate_by_date=entry_gate_by_date,
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
        "rotation_cohort": rot,
        "portfolio": {
            "total_return_pct": port.get("total_return_pct"),
            "cagr_pct": port.get("cagr_pct"),
            "sharpe_ratio": port.get("sharpe_ratio"),
            "max_drawdown_pct": port.get("max_drawdown_pct"),
        },
    }


def run_pool1_seg_last_gate_backtest(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2020-01-02",
    date_end: str | None = None,
    is_end: str = IS_END_DEFAULT,
    oos_h1_start: str = OOS_START_DEFAULT,
    oos_h1_end: str = OOS_H1_END_DEFAULT,
    oos_h2_mode: OosH2Mode = "historical",
    oos_h2_start: str = OOS_H2_START_DEFAULT,
    oos_h2_end: str | None = OOS_H2_END_FORWARD_DEFAULT,
    min_oos_h2_trade_dates: int = MIN_OOS_H2_TRADE_DATES_DEFAULT,
    exclude_fraction: float = 1.0 / 3.0,
) -> dict[str, Any]:
    """POOL1 baseline vs POOL1+seg_last gate · poll_5m · IS / OOS / FULL."""
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

    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    spread_rs_panel = build_daily_spread_rs_panel(close, bench)
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    kbar_cache: dict = {}

    seg_gate = build_seg_last_pass_gate_lookup(
        trade_dates,
        fresh_by_date,
        mono_by_date,
        rs_ratio,
        rs_mom,
        full_dates,
        exclude_fraction=exclude_fraction,
    )
    gate_stats = gate_coverage_stats(
        seg_gate,
        trade_dates,
        fresh_by_date,
        mono_by_date,
        rs_ratio,
        rs_mom,
        full_dates,
    )

    windows: list[tuple[str, str, str]] = [
        ("IS", date_start, is_end),
        ("OOS_H1", oos_h1_start, min(oos_h1_end, date_end)),
        ("FULL", date_start, date_end),
    ]
    if h2_ready and h2_start <= h2_end:
        windows.insert(2, ("OOS_H2", h2_start, h2_end))

    configs = pool1_seg_last_gate_configs()
    rows: list[dict[str, Any]] = []
    for cfg in configs:
        gate = seg_gate if cfg.variant_id == "POOL1-P5M-SEG" else None
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
                    entry_gate_by_date=gate,
                    min_trade_dates=min_td,
                )
            )

    by_variant: dict[str, dict[str, Any]] = {}
    for r in rows:
        by_variant.setdefault(str(r.get("variant_id", "")), {})[str(r.get("label", ""))] = r

    base = by_variant.get("POOL1-P5M", {})
    gated = by_variant.get("POOL1-P5M-SEG", {})
    base_full = base.get("FULL", {})
    gated_full = gated.get("FULL", {})
    base_h1 = base.get("OOS_H1", {})
    gated_h1 = gated.get("OOS_H1", {})
    base_h2 = base.get("OOS_H2", {})
    gated_h2 = gated.get("OOS_H2", {})

    def _delta(a: dict, b: dict, key: str) -> float | None:
        av, bv = a.get(key), b.get(key)
        if av is None or bv is None:
            return None
        return round(float(av) - float(bv), 4)

    comparison = {
        "delta_excess_full_pp": _delta(gated_full, base_full, "mean_excess_pct"),
        "delta_excess_oos_h1_pp": _delta(gated_h1, base_h1, "mean_excess_pct"),
        "delta_excess_oos_h2_pp": _delta(gated_h2, base_h2, "mean_excess_pct"),
        "delta_swaps_full": int(gated_full.get("swaps_total") or 0) - int(base_full.get("swaps_total") or 0),
        "delta_legs_full": int(gated_full.get("n_periods") or 0) - int(base_full.get("n_periods") or 0),
        "delta_cagr_full_pp": round(
            float((gated_full.get("portfolio") or {}).get("cagr_pct") or 0)
            - float((base_full.get("portfolio") or {}).get("cagr_pct") or 0),
            2,
        ),
    }

    beat_h1 = (comparison.get("delta_excess_oos_h1_pp") or 0) >= 0
    beat_h2 = (
        (comparison.get("delta_excess_oos_h2_pp") or 0) >= 0 if h2_ready and gated_h2 else None
    )

    return {
        "schema": "c18acc_pool1_seg_last_gate-v1",
        "topic": "c18acc-pool1-seg-last-gate",
        "timing_mode": "poll_5m",
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_h1": f"{oos_h1_start}..{min(oos_h1_end, date_end)}",
        "oos_h2": f"{h2_start}..{h2_end}" if h2_start <= h2_end else None,
        "oos_h2_mode": h2_mode,
        "gate": {
            "mode": "exclude_bottom_tercile",
            "exclude_fraction": exclude_fraction,
            "pit_cross_section": "daily fresh∪accel pool",
            **gate_stats,
        },
        "variants": by_variant,
        "seg_vs_baseline": comparison,
        "verdict": {
            "seg_beats_baseline_h1": beat_h1,
            "seg_beats_baseline_h2": beat_h2,
            "oos_h2_evaluated": h2_ready,
            "summary": (
                f"POOL1-P5M FULL excess={base_full.get('mean_excess_pct')}% "
                f"n={base_full.get('n_periods')} swaps={base_full.get('swaps_total')} · "
                f"POOL1-P5M-SEG FULL excess={gated_full.get('mean_excess_pct')}% "
                f"n={gated_full.get('n_periods')} swaps={gated_full.get('swaps_total')} · "
                f"ΔFULL={comparison.get('delta_excess_full_pp')}pp · "
                f"ΔH1={comparison.get('delta_excess_oos_h1_pp')}pp · "
                f"ΔH2={comparison.get('delta_excess_oos_h2_pp') if h2_ready else 'pending'} · "
                f"Δswaps={comparison.get('delta_swaps_full')}"
            ),
        },
    }


def render_pool1_seg_last_gate_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict", {})
    cmp_ = payload.get("seg_vs_baseline", {})
    gate = payload.get("gate", {})
    lines = [
        "# C18acc POOL1 · seg_last 低 1/3 gate · poll_5m",
        "",
        "> PIT gate：每日 fresh∪accel 候選池內，排除 seg_last 橫截面最低 1/3",
        "",
        f"- Span: **{payload.get('date_start')} → {payload.get('date_end')}**",
        f"- timing: **{payload.get('timing_mode')}**",
        f"- IS ≤ {payload.get('is_end')} · OOS H1: {payload.get('oos_h1')}",
        f"- Gate: exclude bottom **{float(gate.get('exclude_fraction', 1/3))*100:.0f}%** · "
        f"mean pool {gate.get('mean_pool_size')} → pass {gate.get('mean_pass_size')} "
        f"(excl {gate.get('mean_excluded')}/day)",
        "",
        "## Verdict",
        "",
        f"- {v.get('summary', '')}",
        "",
        "## SEG vs POOL1 baseline",
        "",
        "| window | Δ excess (pp) | Δ legs | Δ swaps |",
        "|--------|---------------|--------|---------|",
        f"| FULL | {cmp_.get('delta_excess_full_pp', '—')} | {cmp_.get('delta_legs_full', '—')} | "
        f"{cmp_.get('delta_swaps_full', '—')} |",
        f"| OOS H1 | {cmp_.get('delta_excess_oos_h1_pp', '—')} | — | — |",
        f"| OOS H2 | {cmp_.get('delta_excess_oos_h2_pp', '—' if v.get('oos_h2_evaluated') else 'pending')} | — | — |",
        f"| Δ CAGR FULL | {cmp_.get('delta_cagr_full_pp', '—')}pp | | |",
        "",
        "## By variant",
        "",
    ]
    for vid in ("POOL1-P5M", "POOL1-P5M-SEG"):
        block = payload.get("variants", {}).get(vid, {})
        lines.append(f"### {vid}")
        lines.append("")
        lines.append("| window | n | mean excess | swaps | CAGR | maxDD |")
        lines.append("|--------|---|-------------|-------|------|-------|")
        for win in ("IS", "OOS_H2", "OOS_H1", "FULL"):
            r = block.get(win)
            if not r:
                continue
            if r.get("error"):
                lines.append(f"| {win} | — | {r.get('error')} | — | — | — |")
                continue
            port = r.get("portfolio") or {}
            lines.append(
                f"| {win} | {r.get('n_periods', '—')} | {r.get('mean_excess_pct', '—')}% | "
                f"{r.get('swaps_total', '—')} | {port.get('cagr_pct', '—')}% | "
                f"{port.get('max_drawdown_pct', '—')}% |"
            )
        lines.append("")
    return "\n".join(lines)
