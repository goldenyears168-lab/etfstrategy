"""RRG mono hold7 · S2 rotation-exit mirror probe (Phase 1 leg-level).

Post-processes simulate_mono_hold7 executed legs with:
  M0 · hold7 close baseline
  M1 · E2 weakening 2d close (no loss gate)
  M2 · S2 mirror weakening 2d + unrealized loss ≤ −5%

Phase 1 does not re-simulate slot recycling after early exit.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from market_breadth_ma import build_breadth_panel
from market_benchmark import load_benchmark_close
from research.backtest.c18acc_rrg_weakening_stop_sweep import (
    DailyQuadExitConfig,
    apply_daily_quad_exit_overlay,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import (
    _summarize,
    build_fresh_mono_calendar,
    simulate_mono_hold7,
)
from research.backtest.rrg_mono_intraday_ab import LENGTH
from research.backtest.rrg_mono_intraday_exit import (
    DEFAULT_EXIT_SWEEP,
    ExitVariantConfig,
    _trading_days_between,
    apply_exit_variant_to_periods,
)
from rrg_rotation import compute_rrg_panel

M2_QUAD_EXIT = DailyQuadExitConfig(
    variant_id="M2",
    label="mono hold7 · S2 mirror (weakening 2d + loss≥5%)",
    exit_quadrants=("weakening",),
    consecutive_days=2,
    min_hold_before_quad_exit=2,
    mtm_threshold_pct=-5.0,
)

HYPOTHESES: dict[str, str] = {
    "H-MONO-S2-1": "M2 Δ mean_excess vs M0 ≥ −0.2pp",
    "H-MONO-S2-2": "M2 quad_exits ≥ 5",
    "H-MONO-S2-3": "M2 triggered legs median_save_vs_orig > 0",
    "H-MONO-S2-4": "M2 Δ mean_excess vs M1 ≥ 0 (loss gate adds value)",
}

TIMELINE_ANCHORS: dict[str, str] = {
    "rrg_mono_hold7": "reports/research/rrg/20260704_rrg_mono_hold7_slots_rrg_timeline_2025_2026.html",
    "c18acc_s2": "reports/research/rrg/20260704_c18acc_s2_slots_rrg_timeline_2025_2026.html",
}


def _enrich_period_prices(
    close,
    periods: list[dict[str, Any]],
    *,
    full_dates: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Add entry_px / exit_px for overlays that expect C18acc-style period dicts."""
    out: list[dict[str, Any]] = []
    for pos in periods:
        p = dict(pos)
        sid = str(p["stock_id"])
        entry = str(p["entry_date"])
        exit_d = str(p["exit_date"])
        if p.get("entry_px") is None and entry in close.index and sid in close.columns:
            px = float(close.at[entry, sid])
            if px == px and px > 0:
                p["entry_px"] = px
        if p.get("exit_px") is None and exit_d in close.index and sid in close.columns:
            px = float(close.at[exit_d, sid])
            if px == px and px > 0:
                p["exit_px"] = px
        if p.get("hold_days") is None and full_dates:
            p["hold_days"] = _trading_days_between(full_dates, entry, exit_d)
        out.append(p)
    return out


def _e2_config() -> ExitVariantConfig:
    for cfg in DEFAULT_EXIT_SWEEP:
        if cfg.variant_id == "E2":
            return cfg
    return ExitVariantConfig(
        "E2",
        "象限轉弱 · 連續2日 · 收盤賣",
        "quad_weak",
        streak_days=2,
        min_hold_days=2,
        max_hold_days=7,
        timing_mode="close",
    )


def _delta_pp(a: dict[str, Any], b: dict[str, Any], key: str = "mean_excess_pct") -> float | None:
    av, bv = a.get(key), b.get(key)
    if av is None or bv is None:
        return None
    return round(float(av) - float(bv), 4)


def _package_variant(
    variant_id: str,
    label: str,
    periods: list[dict[str, Any]],
    summary: dict[str, Any],
    *,
    overlay_kind: str,
) -> dict[str, Any]:
    out = dict(summary)
    out.update(
        {
            "variant_id": variant_id,
            "label": label,
            "overlay_kind": overlay_kind,
            "n_periods": len(periods),
        }
    )
    if out.get("mean_excess_pct") is None and periods:
        out["mean_excess_pct"] = round(sum(p["excess_pct"] for p in periods) / len(periods), 4)
    if periods:
        holds = [p.get("hold_days") for p in periods if p.get("hold_days") is not None]
        out["mean_hold_days"] = round(sum(holds) / len(holds), 2) if holds else None
    return out


def _eval_hypotheses(
    m0: dict[str, Any],
    m1: dict[str, Any],
    m2: dict[str, Any],
    *,
    min_quad_exits: int = 5,
) -> dict[str, Any]:
    d_m2_m0 = _delta_pp(m2, m0)
    d_m2_m1 = _delta_pp(m2, m1)
    quad_exits = int(m2.get("quad_exits") or 0)
    median_save = m2.get("median_save_vs_orig_pct")

    return {
        "H-MONO-S2-1": {
            "pass": d_m2_m0 is not None and d_m2_m0 >= -0.2,
            "value": d_m2_m0,
            "threshold": "≥ −0.2pp vs M0",
        },
        "H-MONO-S2-2": {
            "pass": quad_exits >= min_quad_exits,
            "value": quad_exits,
            "threshold": f"≥ {min_quad_exits}",
        },
        "H-MONO-S2-3": {
            "pass": median_save is not None and float(median_save) > 0,
            "value": median_save,
            "threshold": "> 0 on triggered legs",
        },
        "H-MONO-S2-4": {
            "pass": d_m2_m1 is not None and d_m2_m1 >= 0,
            "value": d_m2_m1,
            "threshold": "≥ 0pp vs M1",
        },
    }


def run_rrg_mono_s2_exit_probe(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    n_slots: int = 3,
    min_quad_exits: int = 5,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    if len(trade_dates) < 2:
        raise ValueError(f"need ≥2 trade dates in [{date_start}, {date_end}], got {len(trade_dates)}")

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    base_periods, _base_summary = simulate_mono_hold7(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        zone_by_date=zone_by_date,
        fresh_by_date=fresh_by_date,
        zone_filter=None,
        entry_price_mode="close",
        n_slots=max(1, int(n_slots)),
    )
    enriched_periods = _enrich_period_prices(close, base_periods, full_dates=full_dates)

    m0_summary = _package_variant(
        "M0",
        "hold7 收盤基線",
        enriched_periods,
        _summarize(enriched_periods),
        overlay_kind="baseline",
    )

    e2 = _e2_config()
    periods_m1, raw_m1 = apply_exit_variant_to_periods(
        conn,
        base_periods=base_periods,
        close=close,
        bench=bench,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        full_dates=full_dates,
        config=e2,
    )
    m1_summary = _package_variant(
        "M1",
        e2.label,
        periods_m1,
        raw_m1,
        overlay_kind="quad_weak_e2",
    )

    periods_m2, raw_m2 = apply_daily_quad_exit_overlay(
        conn,
        base_periods=enriched_periods,
        full_dates=full_dates,
        close=close,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        config=M2_QUAD_EXIT,
    )
    m2_summary = _package_variant(
        "M2",
        M2_QUAD_EXIT.label,
        periods_m2,
        raw_m2,
        overlay_kind="s2_mirror",
    )

    variants = {"M0": m0_summary, "M1": m1_summary, "M2": m2_summary}
    deltas = {
        "M1_vs_M0": _delta_pp(m1_summary, m0_summary),
        "M2_vs_M0": _delta_pp(m2_summary, m0_summary),
        "M2_vs_M1": _delta_pp(m2_summary, m1_summary),
    }
    hypotheses = _eval_hypotheses(
        m0_summary, m1_summary, m2_summary, min_quad_exits=min_quad_exits
    )
    recommend_phase2 = all(h["pass"] for h in hypotheses.values())

    return {
        "schema": "rrg_mono_s2_exit_probe-v1",
        "phase": 1,
        "date_start": date_start,
        "date_end": date_end,
        "n_slots": n_slots,
        "n_trade_dates": len(trade_dates),
        "ssg_note": (
            "進場固定 simulate_mono_hold7 · D4 mono fresh 收盤 · "
            f"{n_slots} 槽 · Phase 1 為 leg 級 overlay，不模擬提早出場釋槽後再接訊號"
        ),
        "hypothesis_statements": HYPOTHESES,
        "m2_spec": M2_QUAD_EXIT.to_dict(),
        "m1_spec": e2.to_dict(),
        "variants": variants,
        "deltas_pp": deltas,
        "hypotheses": hypotheses,
        "recommend_phase2": recommend_phase2,
        "timeline_anchors": TIMELINE_ANCHORS,
    }


def render_rrg_mono_s2_exit_probe_md(payload: dict[str, Any]) -> str:
    lines = [
        "# RRG mono hold7 · S2 rotation-exit mirror probe (Phase 1)",
        "",
        f"區間：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"{payload['n_slots']} 槽 · {payload['n_trade_dates']} 交易日",
        "",
        payload.get("ssg_note", ""),
        "",
        "## M2 spec",
        "",
        "```yaml",
        *[
            f"{k}: {v}"
            for k, v in (payload.get("m2_spec") or {}).items()
        ],
        "```",
        "",
        "## Variants",
        "",
        "| variant | n | mean_excess% | mean_hold | quad_exits | median_save | Δ vs M0 |",
        "|---------|---|--------------|-----------|------------|-------------|---------|",
    ]
    m0 = payload["variants"]["M0"]
    deltas = payload.get("deltas_pp") or {}
    for vid in ("M0", "M1", "M2"):
        v = payload["variants"][vid]
        delta = "—" if vid == "M0" else deltas.get(f"{vid}_vs_M0")
        quad = v.get("quad_exits", "—")
        save = v.get("median_save_vs_orig_pct", "—")
        hold = v.get("mean_hold_days", "—")
        lines.append(
            f"| {vid} | {v.get('n_periods')} | {v.get('mean_excess_pct')} "
            f"| {hold} | {quad} | {save} | {delta} |"
        )

    lines += ["", "## Hypotheses", ""]
    for hid, stmt in (payload.get("hypothesis_statements") or {}).items():
        h = (payload.get("hypotheses") or {}).get(hid) or {}
        mark = "✓" if h.get("pass") else "✗"
        lines.append(f"- {mark} **{hid}**：{stmt} · value={h.get('value')}")

    lines += [
        "",
        f"**Recommend Phase 2 (slot re-sim)**：{'yes' if payload.get('recommend_phase2') else 'no'}",
        "",
        "## Timeline anchors (portfolio KPI · different methodology)",
        "",
    ]
    for k, path in (payload.get("timeline_anchors") or {}).items():
        lines.append(f"- {k}: `{path}`")
    lines += ["", "---", "模組：`scripts/run_rrg_mono_s2_exit_probe.py`", ""]
    return "\n".join(lines)
