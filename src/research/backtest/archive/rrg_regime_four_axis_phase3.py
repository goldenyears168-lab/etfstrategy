"""RRG × Regime four-axis · Phase 3 · POOL1+S2 RH50 entry gate (slot backtest)."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import Any

import pandas as pd

from market_breadth_ma import build_breadth_panel
from market_benchmark import load_benchmark_close
from research.backtest.c18acc_drs_extension_overlay_sweep import (
    OOS_H1_END_DEFAULT,
    OOS_START_DEFAULT,
    build_daily_spread_rs_panel,
)
from research.backtest.c18acc_rotation_force_exit_sweep import (
    _cohort_stats,
    _rotation_cohort_periods,
)
from research.backtest.c18acc_weekday_execution_sweep import build_pool1_s2_config
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, LENGTH
from research.backtest.rrg_mono_score_swap_c import ScoreSwapCConfig, simulate_score_swap_c
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from research.backtest.archive.rrg_regime_four_axis_annot import (
    IS_END,
    IS_START,
    OOS_START,
    _prior_trade_date,
    _universe_rrg_stats_from_quad,
)
from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods
from rrg_rotation import compute_rrg_panel

RH50_THRESHOLD_PCT = 50.0
OOS_MIN_LEGS = 20
OOS_MAX_DELTA_EXCESS_PP = -0.2
MIN_CAPTURE_RATE = 0.25
TOTAL_CAPITAL_NTD = 30_000.0


def build_rotation_health_by_date(
    conn: sqlite3.Connection,
    *,
    length: int = LENGTH,
) -> dict[str, float]:
    """Session date → universe rotation_health_pct (Leading% + Improving%).

    ``length`` is the RRG WMA window used for quadrant classification.
    Official RH50 gate uses ``LENGTH`` (=20); other lengths are research-only.
    """
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn)
    _, _, quad = compute_rrg_panel(close, bench, length=length)
    stats = _universe_rrg_stats_from_quad(quad)
    out: dict[str, float] = {}
    for d, row in stats.items():
        rh = row.get("rotation_health_pct")
        if rh is not None and rh == rh:
            out[str(d)] = float(rh)
    return out


def build_rh50_session_flags(
    trade_dates: list[str],
    full_dates: list[str],
    rotation_health_by_date: dict[str, float],
    *,
    threshold_pct: float = RH50_THRESHOLD_PCT,
) -> dict[str, dict[str, Any]]:
    """Per session T: PIT flag from T-1 rotation_health ≥ threshold."""
    out: dict[str, dict[str, Any]] = {}
    for session in trade_dates:
        prior = _prior_trade_date(full_dates, session)
        rh = rotation_health_by_date.get(str(prior or ""))
        if prior is None or rh is None or rh != rh:
            out[session] = {
                "session_date": session,
                "regime_t1_as_of": prior,
                "rotation_health_pct": rh,
                "rh50_pass": False,
                "missing_t1": prior is None or rh is None,
            }
        else:
            out[session] = {
                "session_date": session,
                "regime_t1_as_of": prior,
                "rotation_health_pct": round(float(rh), 1),
                "rh50_pass": float(rh) >= threshold_pct,
                "missing_t1": False,
            }
    return out


def build_rh50_entry_gate_lookup(
    trade_dates: list[str],
    full_dates: list[str],
    rotation_health_by_date: dict[str, float],
    allow_all_ids: set[str],
    *,
    threshold_pct: float = RH50_THRESHOLD_PCT,
) -> tuple[dict[str, set[str]], dict[str, Any]]:
    """Entry whitelist per day: empty set blocks new legs; superset allows shortlist."""
    flags = build_rh50_session_flags(
        trade_dates, full_dates, rotation_health_by_date, threshold_pct=threshold_pct
    )
    gate: dict[str, set[str]] = {}
    blocked = allowed = missing = 0
    for session, row in flags.items():
        if row.get("missing_t1"):
            gate[session] = set()
            missing += 1
        elif row.get("rh50_pass"):
            gate[session] = set(allow_all_ids)
            allowed += 1
        else:
            gate[session] = set()
            blocked += 1
    n = len(trade_dates) or 1
    stats = {
        "threshold_pct": threshold_pct,
        "blocked_session_days": blocked,
        "allowed_session_days": allowed,
        "missing_t1_days": missing,
        "blocked_share_pct": round(100.0 * blocked / n, 2),
        "allowed_share_pct": round(100.0 * allowed / n, 2),
    }
    return gate, stats


def _calendar_years(date_start: str, date_end: str) -> float:
    d0 = datetime.strptime(date_start, "%Y-%m-%d")
    d1 = datetime.strptime(date_end, "%Y-%m-%d")
    return max((d1 - d0).days / 365.25, 1e-6)


def _delta_pp(a: Any, b: Any) -> float | None:
    if a is None or b is None:
        return None
    return round(float(a) - float(b), 4)


def phase3_variant_specs() -> list[dict[str, Any]]:
    return [
        {
            "variant_id": "R0",
            "label": "POOL1+S2 champion · no Regime entry gate",
            "entry_gate_mode": None,
        },
        {
            "variant_id": "P3-H-RH50",
            "label": "POOL1+S2 · T-1 universe RH≥50% entry block",
            "entry_gate_mode": "rh50",
            "phase2_ref": "P2-H-RH50",
            "phase1_ref": "G-UNIVERSE-RH50",
        },
    ]


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
    fresh_by_date: dict,
    mono_by_date: dict,
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
    )
    leg_sum = summarize_periods(periods)
    port = portfolio_metrics_for_periods(
        conn,
        periods,
        trade_dates,
        total_capital=TOTAL_CAPITAL_NTD,
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
        "max_hold_exits": summary.get("max_hold_exits"),
        "total_entries": summary.get("total_entries"),
        "slot_util_pct": summary.get("slot_util_pct"),
        "rotation_cohort": rot,
        "portfolio": {
            "total_return_pct": port.get("total_return_pct"),
            "cagr_pct": port.get("cagr_pct"),
            "sharpe_ratio": port.get("sharpe_ratio"),
            "max_drawdown_pct": port.get("max_drawdown_pct"),
        },
    }


def _evaluate_variant_pass(
    base_row: dict[str, Any],
    gated_row: dict[str, Any],
    *,
    min_capture: float = MIN_CAPTURE_RATE,
    oos_min_legs: int = OOS_MIN_LEGS,
    oos_max_delta: float = OOS_MAX_DELTA_EXCESS_PP,
) -> dict[str, Any]:
    base_full_n = int(base_row.get("FULL", {}).get("n_periods") or 0)
    gated_full_n = int(gated_row.get("FULL", {}).get("n_periods") or 0)
    capture = round(gated_full_n / base_full_n, 4) if base_full_n > 0 else None

    oos = gated_row.get("OOS") or {}
    base_oos = base_row.get("OOS") or {}
    oos_n = int(oos.get("n_periods") or 0)
    oos_delta = _delta_pp(oos.get("mean_excess_pct"), base_oos.get("mean_excess_pct"))
    full_delta = _delta_pp(
        (gated_row.get("FULL") or {}).get("mean_excess_pct"),
        (base_row.get("FULL") or {}).get("mean_excess_pct"),
    )

    passed = (
        capture is not None
        and capture >= min_capture
        and oos_n >= oos_min_legs
        and oos_delta is not None
        and float(oos_delta) >= oos_max_delta
    )
    return {
        "capture_rate_full": capture,
        "capture_rate_full_pct": round(capture * 100, 2) if capture is not None else None,
        "FULL_delta_excess_pp": full_delta,
        "OOS_delta_excess_pp": oos_delta,
        "OOS_n_periods": oos_n,
        "pass": passed,
    }


def run_rrg_regime_four_axis_phase3(
    conn: sqlite3.Connection,
    *,
    date_start: str = IS_START,
    date_end: str | None = None,
    is_end: str = IS_END,
    oos_start: str = OOS_START,
    oos_end: str = OOS_H1_END_DEFAULT,
    rh50_threshold_pct: float = RH50_THRESHOLD_PCT,
) -> dict[str, Any]:
    """POOL1+S2 slot backtest · R0 vs P3-H-RH50 entry overlay."""
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    if date_end is None:
        date_end = full_dates[-1]
    if date_end > full_dates[-1]:
        date_end = full_dates[-1]

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

    rh_by_date = build_rotation_health_by_date(conn)
    allow_all_ids = {str(c) for c in close.columns}
    rh_gate, gate_stats = build_rh50_entry_gate_lookup(
        trade_dates,
        full_dates,
        rh_by_date,
        allow_all_ids,
        threshold_pct=rh50_threshold_pct,
    )

    base_cfg = build_pool1_s2_config(n_slots=3)
    windows: list[tuple[str, str, str]] = [
        ("IS", date_start, min(is_end, date_end)),
        ("OOS", oos_start, min(oos_end, date_end)),
        ("FULL", date_start, date_end),
    ]

    by_variant: dict[str, dict[str, Any]] = {}
    for spec in phase3_variant_specs():
        vid = str(spec["variant_id"])
        entry_gate = rh_gate if spec.get("entry_gate_mode") == "rh50" else None
        cfg = base_cfg if vid == "R0" else replace(base_cfg, variant_id=vid, label=str(spec["label"]))
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
                entry_gate_by_date=entry_gate,
            )
            by_variant.setdefault(vid, {})[win_label] = row

    base = by_variant.get("R0", {})
    rh50 = by_variant.get("P3-H-RH50", {})
    eval_row = _evaluate_variant_pass(base, rh50)

    winners = []
    if eval_row.get("pass"):
        winners.append(
            {
                "variant_id": "P3-H-RH50",
                "track": "pool1_entry_gate",
                **eval_row,
            }
        )

    return {
        "schema": "rrg_regime_four_axis_phase3-v1",
        "topic": "rrg-regime-four-axis-annot",
        "phase": "phase3_pool1_entry_gate",
        "date_start": date_start,
        "date_end": date_end,
        "is_span": f"{date_start}..{min(is_end, date_end)}",
        "oos_span": f"{oos_start}..{min(oos_end, date_end)}",
        "timing_mode": "poll_5m",
        "candidate_pool": "fresh_union_accel",
        "exit": "S2 weakening 2d + loss≥5%",
        "gate": {
            "rule": f"T-1 universe rotation_health_pct >= {rh50_threshold_pct}",
            "pit": "regime_t1_as_of = prior TW session",
            "entry_only": True,
            **gate_stats,
        },
        "variants": by_variant,
        "evaluation": {
            "P3-H-RH50": eval_row,
            "pass_rules": {
                "min_capture_rate": MIN_CAPTURE_RATE,
                "oos_min_legs": OOS_MIN_LEGS,
                "oos_max_delta_excess_pp": OOS_MAX_DELTA_EXCESS_PP,
            },
        },
        "hypotheses": {
            "P3-H-RH50-OOS": {
                "statement": "POOL1+S2 · RH50 entry gate · OOS 2026H1 Δ mean_excess vs R0 ≥ −0.2pp",
                "OOS_delta_excess_pp": eval_row.get("OOS_delta_excess_pp"),
                "pass": eval_row.get("pass"),
            },
        },
        "n_variants": len(phase3_variant_specs()),
        "n_passed": 1 if eval_row.get("pass") else 0,
        "winners": winners,
    }


def render_rrg_regime_four_axis_phase3_md(payload: dict[str, Any]) -> str:
    gate = payload.get("gate", {})
    ev = (payload.get("evaluation") or {}).get("P3-H-RH50") or {}
    hyp = (payload.get("hypotheses") or {}).get("P3-H-RH50-OOS") or {}
    lines = [
        "# RRG × Regime four-axis · Phase 3 · POOL1 RH50 entry gate",
        "",
        "> Phase 3 wires **P2-H-RH50** onto **rrg_mono POOL1+S2** slot backtest (entry-only).",
        "",
        f"- Span: **{payload.get('date_start')} → {payload.get('date_end')}**",
        f"- IS: {payload.get('is_span')} · OOS: {payload.get('oos_span')}",
        f"- Gate: {gate.get('rule')} · blocked {gate.get('blocked_share_pct')}% sessions",
        "",
        "## Verdict",
        "",
        f"- P3-H-RH50 pass={ev.get('pass')} · capture={ev.get('capture_rate_full_pct')}% "
        f"· OOS Δexcess={ev.get('OOS_delta_excess_pp')}pp · OOS n={ev.get('OOS_n_periods')}",
        f"- Hypothesis P3-H-RH50-OOS: {hyp.get('pass')}",
        "",
        "## Variants",
        "",
        "| variant | window | n legs | mean excess | swaps | Sharpe |",
        "|---------|--------|--------|-------------|-------|--------|",
    ]
    for vid in ("R0", "P3-H-RH50"):
        block = (payload.get("variants") or {}).get(vid, {})
        for win in ("IS", "OOS", "FULL"):
            r = block.get(win)
            if not r or r.get("error"):
                continue
            port = r.get("portfolio") or {}
            lines.append(
                f"| {vid} | {win} | {r.get('n_periods')} | {r.get('mean_excess_pct')} | "
                f"{r.get('swaps_total')} | {port.get('sharpe_ratio')} |"
            )
    lines.extend(
        [
            "",
            "## Pass rules",
            "",
            f"- capture ≥ {MIN_CAPTURE_RATE * 100:.0f}% · OOS n ≥ {OOS_MIN_LEGS} · "
            f"OOS Δexcess ≥ {OOS_MAX_DELTA_EXCESS_PP}pp vs R0",
        ]
    )
    return "\n".join(lines) + "\n"
