"""C18acc S2 selective force-exit · IS/OOS holdout vs S0 baseline."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

import pandas as pd

from market_breadth_ma import build_breadth_panel
from market_benchmark import load_benchmark_close
from research.backtest.c18acc_rotation_force_exit_sweep import (
    _cohort_stats,
    _rotation_cohort_periods,
)
from research.backtest.c18acc_rotation_selective_exit_sweep import (
    rotation_selective_exit_sweep_configs,
)
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, LENGTH
from research.backtest.rrg_mono_score_swap_c import simulate_score_swap_c
from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods
from rrg_rotation import compute_rrg_panel

OOS_START_DEFAULT = "2026-01-01"
OOS_H2_START_DEFAULT = "2026-07-01"
IS_END_DEFAULT = "2025-12-31"
IS_END_H2_DEFAULT = "2026-06-30"
MIN_OOS_TRADE_DATES_DEFAULT = 5


def _calendar_years(date_start: str, date_end: str) -> float:
    d0 = datetime.strptime(date_start, "%Y-%m-%d")
    d1 = datetime.strptime(date_end, "%Y-%m-%d")
    return max((d1 - d0).days / 365.25, 1e-6)


def _run_window(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    label: str,
    s0_cfg,
    s2_cfg,
    c0_cfg,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    min_dates = 3 if label == "OOS_H2" else MIN_OOS_TRADE_DATES_DEFAULT
    if len(trade_dates) < min_dates:
        return {
            "label": label,
            "date_start": date_start,
            "date_end": date_end,
            "error": "insufficient_trade_dates",
            "n_trade_dates": len(trade_dates),
        }
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    kbar_cache: dict = {}

    out: dict[str, Any] = {
        "label": label,
        "date_start": date_start,
        "date_end": date_end,
        "n_trade_dates": len(trade_dates),
        "calendar_years": round(_calendar_years(date_start, date_end), 3),
        "variants": {},
    }
    s0_periods: list[dict[str, Any]] = []
    for cfg in (s0_cfg, s2_cfg):
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
        leg_sum = summarize_periods(periods)
        port = portfolio_metrics_for_periods(
            conn,
            periods,
            trade_dates,
            total_capital=30_000.0,
            n_slots=3,
            close=close,
        )
        rot = _cohort_stats(_rotation_cohort_periods(periods, rs_ratio, rs_mom, full_dates))
        out["variants"][cfg.variant_id] = {
            "variant_id": cfg.variant_id,
            "label": cfg.label,
            "n_periods": summary.get("n_periods"),
            "mean_excess_pct": summary.get("mean_excess_pct"),
            "mean_return_pct": leg_sum.get("mean_return_pct"),
            "mean_bench_pct": leg_sum.get("mean_bench_pct"),
            "win_rate_vs_bench_pct": leg_sum.get("win_rate_vs_bench_pct"),
            "swaps_total": summary.get("swaps_total"),
            "force_exits": summary.get("force_exits"),
            "rotation_cohort": rot,
            "portfolio": {
                "total_return_pct": port.get("total_return_pct"),
                "cagr_pct": port.get("cagr_pct"),
                "ann_mean_return_pct": port.get("ann_mean_return_pct"),
                "sharpe_ratio": port.get("sharpe_ratio"),
                "max_drawdown_pct": port.get("max_drawdown_pct"),
            },
        }
        if cfg.variant_id == "S0":
            s0_periods = periods

    s0 = out["variants"].get("S0") or {}
    s2 = out["variants"].get("S2") or {}
    s0_ex = s0.get("mean_excess_pct")
    s2_ex = s2.get("mean_excess_pct")
    s0_cagr = (s0.get("portfolio") or {}).get("cagr_pct")
    s2_cagr = (s2.get("portfolio") or {}).get("cagr_pct")
    s0_rot = (s0.get("rotation_cohort") or {}).get("mean_return_pct")
    s2_rot = (s2.get("rotation_cohort") or {}).get("mean_return_pct")
    out["delta_s2_vs_s0"] = {
        "mean_excess_pp": round(float(s2_ex) - float(s0_ex), 4)
        if s2_ex is not None and s0_ex is not None
        else None,
        "cagr_pp": round(float(s2_cagr) - float(s0_cagr), 4)
        if s2_cagr is not None and s0_cagr is not None
        else None,
        "rotation_cohort_mean_return_pp": round(float(s2_rot) - float(s0_rot), 4)
        if s2_rot is not None and s0_rot is not None
        else None,
        "portfolio_total_return_pp": round(
            float((s2.get("portfolio") or {}).get("total_return_pct") or 0)
            - float((s0.get("portfolio") or {}).get("total_return_pct") or 0),
            4,
        ),
    }
    return out


def _eval_hypotheses(payload: dict[str, Any]) -> dict[str, Any]:
    oos = payload.get("oos") or {}
    oos_delta = oos.get("delta_s2_vs_s0") or {}
    oos_s2 = (oos.get("variants") or {}).get("S2") or {}
    oos_n = oos_s2.get("n_periods") or 0
    ex_pp = oos_delta.get("mean_excess_pp")
    cagr_pp = oos_delta.get("cagr_pp")
    rot_pp = oos_delta.get("rotation_cohort_mean_return_pp")

    hypos = {
        "H-S2-OOS-1": {
            "statement": "OOS Δ mean_excess (S2−S0) ≥ −0.2pp",
            "pass": ex_pp is not None and float(ex_pp) >= -0.2,
            "value": ex_pp,
        },
        "H-S2-OOS-2": {
            "statement": "OOS portfolio CAGR Δ (S2−S0) ≥ 0",
            "pass": cagr_pp is not None and float(cagr_pp) >= 0,
            "value": cagr_pp,
        },
        "H-S2-OOS-3": {
            "statement": "OOS n_periods ≥ 20",
            "pass": int(oos_n) >= 20,
            "value": oos_n,
        },
        "H-S2-OOS-4": {
            "statement": "OOS rotation cohort mean_ret Δ ≥ 0",
            "pass": rot_pp is not None and float(rot_pp) >= 0,
            "value": rot_pp,
        },
    }
    all_pass = all(h.get("pass") for h in hypos.values())
    return {"hypotheses": hypos, "all_pass": all_pass}


def run_c18acc_s2_oos_holdout(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-30",
    is_end: str = IS_END_DEFAULT,
    oos_start: str = OOS_START_DEFAULT,
    include_h2_oos: bool = False,
) -> dict[str, Any]:
    configs = {c.variant_id: c for c in rotation_selective_exit_sweep_configs()}
    s0_cfg = configs["S0"]
    s2_cfg = configs["S2"]
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")

    is_block = _run_window(
        conn,
        date_start=date_start,
        date_end=is_end,
        label="IS",
        s0_cfg=s0_cfg,
        s2_cfg=s2_cfg,
        c0_cfg=c0_cfg,
    )
    oos_block = _run_window(
        conn,
        date_start=oos_start,
        date_end=date_end,
        label="OOS",
        s0_cfg=s0_cfg,
        s2_cfg=s2_cfg,
        c0_cfg=c0_cfg,
    )
    full_block = _run_window(
        conn,
        date_start=date_start,
        date_end=date_end,
        label="FULL",
        s0_cfg=s0_cfg,
        s2_cfg=s2_cfg,
        c0_cfg=c0_cfg,
    )

    h2_block = None
    if include_h2_oos and oos_start < OOS_H2_START_DEFAULT <= date_end:
        h2_block = _run_window(
            conn,
            date_start=OOS_H2_START_DEFAULT,
            date_end=date_end,
            label="OOS_H2",
            s0_cfg=s0_cfg,
            s2_cfg=s2_cfg,
            c0_cfg=c0_cfg,
        )

    payload: dict[str, Any] = {
        "topic": "c18acc-rotation-exit",
        "variant_adopt": "S2",
        "s2_spec": {
            "quad_force_exit_mode": "weakening",
            "quad_force_exit_min_days": 2,
            "quad_force_exit_loss_pct": -0.05,
            "quad_force_exit_requires_min_hold": True,
        },
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_start": oos_start,
        "oos_note": (
            f"OOS={oos_start}..{date_end} · DB 最新 {date_end}"
            + (
                f" · H2（{OOS_H2_START_DEFAULT}..{date_end}）"
                f" n_dates={(h2_block or {}).get('n_trade_dates', 0)}"
                if h2_block
                else ""
            )
        ),
        "is": is_block,
        "oos": oos_block,
        "full": full_block,
    }
    if h2_block is not None:
        payload["oos_h2"] = h2_block
    eval_result = _eval_hypotheses(payload)
    payload["graduation"] = {
        **eval_result,
        "recommend_adopt": bool(eval_result.get("all_pass")),
        "gates": {
            "G2_oos_holdout": "passed" if eval_result.get("all_pass") else "failed",
        },
    }
    return payload


def render_c18acc_s2_oos_holdout_md(payload: dict[str, Any]) -> str:
    lines = [
        f"# C18acc S2 · OOS holdout vs S0",
        "",
        f"**IS**：{payload.get('date_start')} .. {payload.get('is_end')}",
        f"**OOS**：{payload.get('oos_start')} .. {payload.get('date_end')}",
        f"*{payload.get('oos_note')}*",
        "",
        "## S2 spec",
        "",
        "```yaml",
        "quad_force_exit_mode: weakening",
        "quad_force_exit_min_days: 2",
        "quad_force_exit_loss_pct: -0.05",
        "```",
        "",
    ]

    def _table(block: dict[str, Any], title: str) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if block.get("error"):
            lines.append(f"_error: {block['error']} · n_dates={block.get('n_trade_dates')}_")
            lines.append("")
            return
        delta = block.get("delta_s2_vs_s0") or {}
        lines.append(
            "| variant | n | mean_excess% | CAGR% | total_ret% | Sharpe | MaxDD% | force_exits |"
        )
        lines.append("|---------|---|--------------|-------|------------|--------|--------|-------------|")
        for vid in ("S0", "S2"):
            v = (block.get("variants") or {}).get(vid) or {}
            p = v.get("portfolio") or {}
            lines.append(
                f"| {vid} | {v.get('n_periods')} | {v.get('mean_excess_pct')} | "
                f"{p.get('cagr_pct')} | {p.get('total_return_pct')} | "
                f"{p.get('sharpe_ratio')} | {p.get('max_drawdown_pct')} | {v.get('force_exits')} |"
            )
        lines.append("")
        lines.append(
            f"Δ S2−S0：mean_excess **{delta.get('mean_excess_pp')}pp** · "
            f"CAGR **{delta.get('cagr_pp')}pp** · "
            f"total_ret **{delta.get('portfolio_total_return_pp')}pp** · "
            f"rotation_cohort **{delta.get('rotation_cohort_mean_return_pp')}pp**"
        )
        lines.append("")

    _table(payload.get("is") or {}, "In-sample")
    _table(payload.get("oos") or {}, "Out-of-sample")
    if payload.get("oos_h2"):
        _table(payload.get("oos_h2") or {}, "Out-of-sample · 2026 H2 only")
    _table(payload.get("full") or {}, "Full window")

    grad = payload.get("graduation") or {}
    lines.append("## Graduation hypotheses")
    lines.append("")
    for hid, h in (grad.get("hypotheses") or {}).items():
        mark = "✓" if h.get("pass") else "✗"
        lines.append(f"- {mark} **{hid}**：{h.get('statement')} · value={h.get('value')}")
    lines.append("")
    lines.append(
        f"**Recommend adopt**：{'yes' if grad.get('recommend_adopt') else 'no'} · "
        f"G2_oos_holdout={((grad.get('gates') or {}).get('G2_oos_holdout'))}"
    )
    lines.append("")
    lines.append("---")
    lines.append("模組：`scripts/run_c18acc_s2_oos_holdout.py`")
    lines.append("")
    return "\n".join(lines)
