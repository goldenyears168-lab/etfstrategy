"""C18acc POOL1+S2 · F1 Friday swap margin parameter sweep (research)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

import pandas as pd

from market_breadth_ma import build_breadth_panel
from market_benchmark import load_benchmark_close
from research.backtest.c18acc_weekday_execution_sweep import (
    IS_END_DEFAULT,
    OOS_START_DEFAULT,
    _delta_pp,
    _run_variant_window,
    build_pool1_s2_config,
)
from research.backtest.c18acc_weekday_gate import WeekdayGateConfig
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, LENGTH
from research.backtest.rrg_mono_score_swap_c import build_daily_spread_rs_panel
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_rotation import compute_rrg_panel

# baseline weekday margin = 0.05 · 週五放寬 grid（含等於 baseline 與略嚴）
FRIDAY_SWAP_MARGIN_GRID: tuple[float, ...] = (
    0.0,
    0.01,
    0.02,
    0.025,
    0.03,
    0.035,
    0.04,
    0.045,
    0.05,
    0.06,
    0.07,
    0.08,
)
BASELINE_MARGIN = 0.05
OOS_PASS_DELTA_PP = -0.2


def f1_variant_id(margin: float) -> str:
    return f"F1m{int(round(margin * 1000)):03d}"


def friday_swap_margin_gate(margin: float) -> WeekdayGateConfig:
    return WeekdayGateConfig(
        variant_id=f1_variant_id(margin),
        friday_swap_margin=float(margin),
    )


def friday_swap_margin_gates(
    *,
    margins: tuple[float, ...] = FRIDAY_SWAP_MARGIN_GRID,
    include_w0: bool = True,
) -> list[WeekdayGateConfig]:
    gates: list[WeekdayGateConfig] = []
    if include_w0:
        gates.append(WeekdayGateConfig(variant_id="W0"))
    gates.extend(friday_swap_margin_gate(m) for m in margins)
    return gates


def _calendar_years(date_start: str, date_end: str) -> float:
    d0 = datetime.strptime(date_start, "%Y-%m-%d")
    d1 = datetime.strptime(date_end, "%Y-%m-%d")
    return max((d1 - d0).days / 365.25, 1e-6)


def _pick_best_f1(
    by_gate: dict[str, dict[str, Any]],
    *,
    w0_full_ex: float,
    w0_oos_ex: float,
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score = w0_full_ex + w0_oos_ex - 999
    for vid, wins in by_gate.items():
        if not vid.startswith("F1m"):
            continue
        full = wins.get("FULL") or {}
        oos = wins.get("OOS") or {}
        full_ex = full.get("mean_excess_pct")
        oos_ex = oos.get("mean_excess_pct")
        if full_ex is None or oos_ex is None:
            continue
        d_full = float(full_ex) - w0_full_ex
        d_oos = float(oos_ex) - w0_oos_ex
        if d_full < OOS_PASS_DELTA_PP or d_oos < OOS_PASS_DELTA_PP:
            continue
        score = float(full_ex) + float(oos_ex)
        if score > best_score:
            best_score = score
            margin = (full.get("gate") or {}).get("friday_swap_margin")
            best = {
                "variant_id": vid,
                "friday_swap_margin": margin,
                "full_mean_excess_pct": full_ex,
                "oos_mean_excess_pct": oos_ex,
                "full_delta_pp": round(d_full, 4),
                "oos_delta_pp": round(d_oos, 4),
                "swaps_total": full.get("swaps_total"),
                "n_periods": full.get("n_periods"),
            }
    return best


def run_c18acc_friday_swap_margin_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2020-01-02",
    date_end: str = "2026-07-03",
    is_end: str = IS_END_DEFAULT,
    oos_start: str = OOS_START_DEFAULT,
    n_slots: int = 3,
    margins: tuple[float, ...] = FRIDAY_SWAP_MARGIN_GRID,
) -> dict[str, Any]:
    cfg = build_pool1_s2_config(n_slots=n_slots)
    gates = friday_swap_margin_gates(margins=margins, include_w0=True)
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
        ("IS", "2024-01-01", is_end),
        ("OOS", oos_start, date_end),
        ("FULL", date_start, date_end),
    ]

    rows: list[dict[str, Any]] = []
    for gate in gates:
        for win_label, w_start, w_end in windows:
            if w_start > w_end:
                continue
            rows.append(
                _run_variant_window(
                    conn,
                    cfg=cfg,
                    gate=gate,
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
            )

    by_gate: dict[str, dict[str, Any]] = {}
    for r in rows:
        gid = str(r.get("gate_variant_id", ""))
        by_gate.setdefault(gid, {})[str(r.get("label", ""))] = r

    w0_full = by_gate.get("W0", {}).get("FULL") or {}
    w0_oos = by_gate.get("W0", {}).get("OOS") or {}
    w0_full_ex = float(w0_full.get("mean_excess_pct") or 0)
    w0_oos_ex = float(w0_oos.get("mean_excess_pct") or 0)
    w0_full_n = int(w0_full.get("n_periods") or 0)

    grid_rows: list[dict[str, Any]] = []
    for margin in margins:
        vid = f1_variant_id(margin)
        full = by_gate.get(vid, {}).get("FULL") or {}
        oos = by_gate.get(vid, {}).get("OOS") or {}
        is_row = by_gate.get(vid, {}).get("IS") or {}
        d_full = _delta_pp(full.get("mean_excess_pct"), w0_full.get("mean_excess_pct"))
        d_oos = _delta_pp(oos.get("mean_excess_pct"), w0_oos.get("mean_excess_pct"))
        d_is = _delta_pp(is_row.get("mean_excess_pct"), (by_gate.get("W0", {}).get("IS") or {}).get("mean_excess_pct"))
        pass_both = (
            d_full is not None
            and d_oos is not None
            and float(d_full) >= OOS_PASS_DELTA_PP
            and float(d_oos) >= OOS_PASS_DELTA_PP
        )
        grid_rows.append(
            {
                "variant_id": vid,
                "friday_swap_margin": margin,
                "baseline_margin": BASELINE_MARGIN,
                "n_periods": full.get("n_periods"),
                "capture_rate_pct": round(int(full.get("n_periods") or 0) / max(w0_full_n, 1) * 100, 2),
                "full_mean_excess_pct": full.get("mean_excess_pct"),
                "full_delta_pp": d_full,
                "is_delta_pp": d_is,
                "oos_mean_excess_pct": oos.get("mean_excess_pct"),
                "oos_delta_pp": d_oos,
                "swaps_total": full.get("swaps_total"),
                "max_hold_exits": full.get("max_hold_exits"),
                "rotation_mean_return_pct": (full.get("rotation_cohort") or {}).get("mean_return_pct"),
                "portfolio_sharpe": (full.get("portfolio") or {}).get("sharpe_ratio"),
                "portfolio_mdd_pct": (full.get("portfolio") or {}).get("max_drawdown_pct"),
                "pass_threshold": pass_both,
            }
        )

    grid_rows.sort(
        key=lambda r: (
            0 if r.get("pass_threshold") else 1,
            -(float(r.get("full_mean_excess_pct") or -999) + float(r.get("oos_mean_excess_pct") or -999)),
        )
    )

    best = _pick_best_f1(by_gate, w0_full_ex=w0_full_ex, w0_oos_ex=w0_oos_ex)

    return {
        "schema": "c18acc_friday_swap_margin_sweep-v1",
        "topic": "c18acc-weekday-execution",
        "phase": "f1_margin_grid",
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_start": oos_start,
        "n_slots": n_slots,
        "baseline_variant": "W0",
        "baseline_margin": BASELINE_MARGIN,
        "pass_delta_pp": OOS_PASS_DELTA_PP,
        "margins_grid": list(margins),
        "calendar_years": round(_calendar_years(date_start, date_end), 3),
        "w0_baseline": {
            "FULL": {
                "n_periods": w0_full.get("n_periods"),
                "mean_excess_pct": w0_full.get("mean_excess_pct"),
                "swaps_total": w0_full.get("swaps_total"),
            },
            "OOS": {
                "n_periods": w0_oos.get("n_periods"),
                "mean_excess_pct": w0_oos.get("mean_excess_pct"),
            },
        },
        "grid_ranked": grid_rows,
        "best_passing": best,
        "by_gate": by_gate,
        "rows": rows,
    }


def render_c18acc_friday_swap_margin_sweep_md(payload: dict[str, Any]) -> str:
    w0 = payload.get("w0_baseline") or {}
    w0_full = w0.get("FULL") or {}
    w0_oos = w0.get("OOS") or {}
    best = payload.get("best_passing")
    lines = [
        "# C18acc POOL1+S2 · F1 Friday swap margin sweep",
        "",
        f"區間：**{payload['date_start']}** → **{payload['date_end']}** · "
        f"IS ≤ {payload['is_end']} · OOS ≥ {payload['oos_start']}",
        "",
        f"- Baseline weekday margin：**{payload.get('baseline_margin')}** · "
        f"週五 grid：{payload.get('margins_grid')}",
        f"- Pass：FULL/OOS Δ vs W0 ≥ **{payload.get('pass_delta_pp')}** pp",
        "",
        f"W0 FULL：n={w0_full.get('n_periods')} · excess={w0_full.get('mean_excess_pct')}% · "
        f"swaps={w0_full.get('swaps_total')}",
        f"W0 OOS：n={w0_oos.get('n_periods')} · excess={w0_oos.get('mean_excess_pct')}%",
        "",
    ]
    if best:
        lines.append(
            f"**Best passing：** `{best.get('variant_id')}` · "
            f"margin={best.get('friday_swap_margin')} · "
            f"FULL Δ={best.get('full_delta_pp')}pp · OOS Δ={best.get('oos_delta_pp')}pp · "
            f"swaps={best.get('swaps_total')}"
        )
    else:
        lines.append("**Best passing：** none（無 variant 同時通過 FULL/OOS 門檻）")
    lines += [
        "",
        "## Grid ranked (FULL + OOS)",
        "",
        "| margin | variant | n | FULL excess | Δ FULL | Δ OOS | swaps | pass |",
        "|--------|---------|---|-------------|--------|-------|-------|------|",
    ]
    for r in payload.get("grid_ranked") or []:
        lines.append(
            f"| {r.get('friday_swap_margin')} | {r.get('variant_id')} | {r.get('n_periods')} | "
            f"{r.get('full_mean_excess_pct')} | {r.get('full_delta_pp')} | {r.get('oos_delta_pp')} | "
            f"{r.get('swaps_total')} | {r.get('pass_threshold')} |"
        )
    lines += ["", "---", "模組：`scripts/run_c18acc_friday_swap_margin_sweep.py`", ""]
    return "\n".join(lines)
