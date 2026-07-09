"""C18acc × ABC v3+f1 · Phase1 fixed-weight partitioned NAV (research only).

Independent capital pools · no filter merge · no strategy spec change.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime
from typing import Any

from backtest_standard_config import comparison_notional_ntd, load_backtest_standard_config
from research.backtest.archive.abc_v3_slot_sweep import (
    ABC_V3_SLOT_CHAMPION,
    _build_abc_v3_intraday_panel,
    apply_score_spec,
    collect_abc_v3_skip0900_period_candidates,
    order_periods_for_fill,
)
from research.backtest.archive.c18acc_abc_dual_sleeve_phase0 import (
    _pearson,
    compute_daily_return_correlation,
)
from research.backtest.archive.c18acc_abc_v3_f1_comparison import (
    C18ACC_SLOTS,
    _portfolio_block,
    select_executed_slot_periods,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_score_swap_c import build_c18acc_s2_executed_legs_for_timeline
from research.backtest.slot_portfolio_metrics import _max_drawdown_pct
from research.backtest.triple_wma_pullback_sweep import matches_w3_rv_hl_abc_v3_f1

DEFAULT_C18_WEIGHT = 0.70
DEFAULT_ABC_WEIGHT = 0.30
PHASE1_WEIGHT_GRID: tuple[tuple[float, float], ...] = (
    (0.85, 0.15),
    (0.80, 0.20),
    (0.70, 0.30),
    (0.60, 0.40),
)
IS_END_DEFAULT = "2025-12-31"
OOS_H1_START = "2026-01-01"
OOS_H1_END = "2026-06-30"


def _nav_equity_map(daily_nav: list[dict[str, Any]]) -> dict[str, float]:
    return {str(r["date"]): float(r["equity_ntd"]) for r in daily_nav if r.get("date")}


def _metrics_from_nav_series(
    trade_dates: list[str],
    daily_nav: list[dict[str, Any]],
    *,
    total_capital: float,
    date_start: str | None = None,
    date_end: str | None = None,
) -> dict[str, Any]:
    """Portfolio metrics from daily_nav · optional sub-window."""
    nav_map = _nav_equity_map(daily_nav)
    dates = [d for d in trade_dates if d in nav_map]
    if date_start:
        dates = [d for d in dates if d >= date_start]
    if date_end:
        dates = [d for d in dates if d <= date_end]
    if not dates:
        return {"error": "empty_window", "n_trade_dates": 0}

    equities = [nav_map[d] for d in dates]
    start_eq = equities[0]
    final = equities[-1]
    daily_returns: list[float] = []
    for i in range(1, len(equities)):
        prev = equities[i - 1]
        if prev > 0:
            daily_returns.append((equities[i] / prev - 1.0) * 100.0)

    n_dr = len(daily_returns)
    years = len(dates) / 252.0
    total_ret = (final / start_eq - 1.0) * 100.0 if start_eq > 0 else 0.0
    cagr = None
    if years > 0 and final > 0 and start_eq > 0:
        cagr = round(((final / start_eq) ** (1.0 / years) - 1.0) * 100.0, 4)

    mean_dr = sum(daily_returns) / n_dr if n_dr else None
    sharpe = None
    if n_dr > 1 and mean_dr is not None:
        var = sum((x - mean_dr) ** 2 for x in daily_returns) / (n_dr - 1)
        std_dr = math.sqrt(var)
        sharpe = round(mean_dr / std_dr * math.sqrt(252.0), 4) if std_dr > 0 else None

    return {
        "date_start": dates[0],
        "date_end": dates[-1],
        "n_trade_dates": len(dates),
        "total_capital_ntd": round(total_capital, 2),
        "final_equity_ntd": round(final, 2),
        "total_return_pct": round(total_ret, 4),
        "cagr_pct": cagr,
        "mean_daily_return_pct": round(mean_dr, 6) if mean_dr is not None else None,
        "ann_mean_return_pct": round(mean_dr * 252.0, 4) if mean_dr is not None else None,
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": _max_drawdown_pct(equities, start_eq),
    }


def combine_fixed_weight_nav(
    trade_dates: list[str],
    c18_daily_nav: list[dict[str, Any]],
    abc_daily_nav: list[dict[str, Any]],
    *,
    total_capital: float,
    c18_weight: float = DEFAULT_C18_WEIGHT,
    abc_weight: float = DEFAULT_ABC_WEIGHT,
) -> tuple[list[dict[str, Any]], list[float]]:
    """Linear capital split · each sleeve simulated at full notional then scaled."""
    w_c = c18_weight
    w_a = abc_weight
    if abs(w_c + w_a - 1.0) > 1e-6:
        raise ValueError(f"weights must sum to 1.0 · got {w_c}+{w_a}")
    nav_c = _nav_equity_map(c18_daily_nav)
    nav_a = _nav_equity_map(abc_daily_nav)
    cap_c = total_capital * w_c
    cap_a = total_capital * w_a

    combo_nav: list[dict[str, Any]] = []
    equities: list[float] = []
    for d in trade_dates:
        eq_c = nav_c.get(d, cap_c)
        eq_a = nav_a.get(d, cap_a)
        eq = w_c * eq_c + w_a * eq_a
        equities.append(eq)
        combo_nav.append({"date": d, "equity_ntd": round(eq, 2)})
    return combo_nav, equities


def run_partitioned_combo_metrics(
    trade_dates: list[str],
    c18_daily_nav: list[dict[str, Any]],
    abc_daily_nav: list[dict[str, Any]],
    *,
    total_capital: float,
    c18_weight: float,
    abc_weight: float,
    c18_sleeve_metrics: dict[str, Any] | None = None,
    abc_sleeve_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    combo_nav, _ = combine_fixed_weight_nav(
        trade_dates,
        c18_daily_nav,
        abc_daily_nav,
        total_capital=total_capital,
        c18_weight=c18_weight,
        abc_weight=abc_weight,
    )
    full = _metrics_from_nav_series(trade_dates, combo_nav, total_capital=total_capital)
    is_m = _metrics_from_nav_series(
        trade_dates,
        combo_nav,
        total_capital=total_capital,
        date_start=trade_dates[0],
        date_end=IS_END_DEFAULT,
    )
    oos_m = _metrics_from_nav_series(
        trade_dates,
        combo_nav,
        total_capital=total_capital,
        date_start=OOS_H1_START,
        date_end=OOS_H1_END,
    )
    corr = compute_daily_return_correlation(c18_daily_nav, abc_daily_nav)
    return {
        "mode": "partitioned_fixed_weight",
        "c18_weight": c18_weight,
        "abc_weight": abc_weight,
        "c18_capital_ntd": round(total_capital * c18_weight, 2),
        "abc_capital_ntd": round(total_capital * abc_weight, 2),
        "windows": {
            "FULL": full,
            "IS": is_m,
            "OOS_H1": oos_m,
        },
        "daily_return_corr": corr.get("daily_return_corr"),
        "sleeves": {
            "c18acc": c18_sleeve_metrics,
            "abc_f1": abc_sleeve_metrics,
        },
        "daily_nav": combo_nav,
    }


def phase1_verdict(
    combo: dict[str, Any],
    c18_baseline: dict[str, Any],
    *,
    c18_weight: float = DEFAULT_C18_WEIGHT,
    abc_weight: float = DEFAULT_ABC_WEIGHT,
) -> dict[str, Any]:
    """Preregistered Phase1 · combo must beat C18-only on FULL daily + CAGR."""
    c_full = (combo.get("windows") or {}).get("FULL") or {}
    b_full = (c18_baseline.get("windows") or {}).get("FULL") or {}
    c_oos = (combo.get("windows") or {}).get("OOS_H1") or {}
    b_oos = (c18_baseline.get("windows") or {}).get("OOS_H1") or {}

    def _delta(a: dict, b: dict, key: str) -> float | None:
        av, bv = a.get(key), b.get(key)
        if av is None or bv is None:
            return None
        return round(float(av) - float(bv), 4)

    delta_daily = _delta(c_full, b_full, "mean_daily_return_pct")
    delta_cagr = _delta(c_full, b_full, "cagr_pct")
    delta_total = _delta(c_full, b_full, "total_return_pct")
    delta_oos_daily = _delta(c_oos, b_oos, "mean_daily_return_pct")

    beat_daily = delta_daily is not None and delta_daily > 0
    beat_cagr = delta_cagr is not None and delta_cagr > 0
    adopt = beat_daily and beat_cagr

    summary = (
        f"{'ADOPT_DUAL_SLEEVE' if adopt else 'NO_GO_DUAL_SLEEVE'} · "
        f"{int(c18_weight*100)}/{int(abc_weight*100)} · "
        f"Δdaily={delta_daily}pp · ΔCAGR={delta_cagr}pp · Δtotal={delta_total}pp · "
        f"ΔOOS_H1_daily={delta_oos_daily}pp"
    )
    if not adopt:
        fails = []
        if not beat_daily:
            fails.append("mean_daily")
        if not beat_cagr:
            fails.append("cagr")
        summary += f" · failed={','.join(fails)}"

    return {
        "adopt_dual_sleeve": adopt,
        "default_weights": {"c18": c18_weight, "abc": abc_weight},
        "delta_full_mean_daily_pp": delta_daily,
        "delta_full_cagr_pp": delta_cagr,
        "delta_full_total_return_pp": delta_total,
        "delta_oos_h1_mean_daily_pp": delta_oos_daily,
        "checks": {
            "beat_c18_mean_daily_full": beat_daily,
            "beat_c18_cagr_full": beat_cagr,
        },
        "summary": summary,
    }


def build_phase1_payload(
    *,
    date_start: str,
    date_end: str,
    n_trade_dates: int,
    notional: float,
    c18_daily_nav: list[dict[str, Any]],
    abc_daily_nav: list[dict[str, Any]],
    c18_sleeve_metrics: dict[str, Any] | None = None,
    abc_sleeve_metrics: dict[str, Any] | None = None,
    event_level: dict[str, Any] | None = None,
    trade_dates: list[str],
    weight_grid: tuple[tuple[float, float], ...] = PHASE1_WEIGHT_GRID,
    source: str = "live",
    runtime_s: float | None = None,
) -> dict[str, Any]:
    trade_dates = list(trade_dates)
    baselines: dict[str, Any] = {}
    baselines["c18acc_only"] = run_partitioned_combo_metrics(
        trade_dates,
        c18_daily_nav,
        abc_daily_nav,
        total_capital=notional,
        c18_weight=1.0,
        abc_weight=0.0,
        c18_sleeve_metrics=c18_sleeve_metrics,
        abc_sleeve_metrics=abc_sleeve_metrics,
    )
    baselines["abc_f1_only"] = run_partitioned_combo_metrics(
        trade_dates,
        c18_daily_nav,
        abc_daily_nav,
        total_capital=notional,
        c18_weight=0.0,
        abc_weight=1.0,
        c18_sleeve_metrics=c18_sleeve_metrics,
        abc_sleeve_metrics=abc_sleeve_metrics,
    )

    combos: dict[str, Any] = {}
    for w_c, w_a in weight_grid:
        key = f"c18_{int(w_c*100)}_abc_{int(w_a*100)}"
        combos[key] = run_partitioned_combo_metrics(
            trade_dates,
            c18_daily_nav,
            abc_daily_nav,
            total_capital=notional,
            c18_weight=w_c,
            abc_weight=w_a,
            c18_sleeve_metrics=c18_sleeve_metrics,
            abc_sleeve_metrics=abc_sleeve_metrics,
        )

    default_key = f"c18_{int(DEFAULT_C18_WEIGHT*100)}_abc_{int(DEFAULT_ABC_WEIGHT*100)}"
    default_combo = combos[default_key]
    verdict = phase1_verdict(default_combo, baselines["c18acc_only"])

    best_key = default_key
    best_daily = (default_combo.get("windows") or {}).get("FULL", {}).get(
        "mean_daily_return_pct"
    )
    for key, block in combos.items():
        md = (block.get("windows") or {}).get("FULL", {}).get("mean_daily_return_pct")
        if md is not None and (best_daily is None or md > best_daily):
            best_daily = md
            best_key = key

    return {
        "schema": "c18acc_abc_dual_sleeve_phase1-v1",
        "topic": "c18acc-abc-dual-sleeve",
        "phase": "phase1_partitioned_nav",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "date_start": date_start,
        "date_end": date_end,
        "n_trade_dates": n_trade_dates,
        "notional_ntd": notional,
        "contract": {
            "mode": "partitioned_fixed_weight",
            "c18acc": f"rrg-mono-swap-accel · {C18ACC_SLOTS} slots",
            "abc": f"abc-v3-f1-pullback · {ABC_V3_SLOT_CHAMPION} slots",
            "default_weights": f"{int(DEFAULT_C18_WEIGHT*100)}/{int(DEFAULT_ABC_WEIGHT*100)}",
            "note": "獨立資金池 · 不混篩選器 · 不改 strategy spec",
            "abc_sleeve_nav_layer": "portfolio_5slot",
            "abc_adopted_edge_layer": "event_level_3d",
        },
        "event_level": event_level,
        "baselines": baselines,
        "combos": combos,
        "default_combo_key": default_key,
        "best_combo_by_full_daily": best_key,
        "verdict": verdict,
        "runtime_s": runtime_s,
    }


def build_phase1_from_comparison_json(
    path: str,
    *,
    weight_grid: tuple[tuple[float, float], ...] = PHASE1_WEIGHT_GRID,
) -> dict[str, Any]:
    payload = json.loads(open(path, encoding="utf-8").read())
    notional = float((payload.get("comparison_contract") or {}).get("notional_ntd") or 100_000)
    strategies = payload.get("strategies") or {}
    c18_nav = (strategies.get("rrg-mono-swap-accel") or {}).get("portfolio", {}).get("daily_nav") or []
    abc_nav = (strategies.get("abc-v3-f1-pullback") or {}).get("portfolio", {}).get("daily_nav") or []
    trade_dates = sorted({str(r["date"]) for r in c18_nav} | {str(r["date"]) for r in abc_nav})
    return build_phase1_payload(
        date_start=str(payload.get("date_start") or trade_dates[0]),
        date_end=str(payload.get("date_end") or trade_dates[-1]),
        n_trade_dates=int(payload.get("n_trade_dates") or len(trade_dates)),
        notional=notional,
        c18_daily_nav=c18_nav,
        abc_daily_nav=abc_nav,
        c18_sleeve_metrics=strategies.get("rrg-mono-swap-accel", {}).get("portfolio"),
        abc_sleeve_metrics=strategies.get("abc-v3-f1-pullback", {}).get("portfolio"),
        event_level=payload.get("event_level"),
        trade_dates=trade_dates,
        weight_grid=weight_grid,
        source=f"comparison_json:{path}",
    )


def run_c18acc_abc_dual_sleeve_phase1(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str | None = None,
    c18_weight: float = DEFAULT_C18_WEIGHT,
    abc_weight: float = DEFAULT_ABC_WEIGHT,
    weight_grid: tuple[tuple[float, float], ...] | None = None,
) -> dict[str, Any]:
    """Live partitioned sim · split capital at source (slot caps scale)."""
    t0 = time.perf_counter()
    std = load_backtest_standard_config()
    notional = comparison_notional_ntd()
    cost_model = dict(std.cost_model)

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]

    cap_c18 = notional * c18_weight
    cap_abc = notional * abc_weight

    c18_legs, _, _, _ = build_c18acc_s2_executed_legs_for_timeline(
        conn,
        trade_dates,
        n_slots=C18ACC_SLOTS,
        capital_ntd=cap_c18 / C18ACC_SLOTS,
        use_close_timing=True,
    )
    c18_periods = [
        {
            "entry_date": str(lg["entry_date"]),
            "exit_date": str(lg["exit_date"]),
            "stock_id": str(lg["stock_id"]),
        }
        for lg in c18_legs
    ]
    c18_pf = _portfolio_block(
        conn,
        close,
        trade_dates,
        c18_periods,
        total_capital=cap_c18,
        n_slots=C18ACC_SLOTS,
        cost_model=cost_model,
    )

    frames, trajectories, panel_meta, stock_maps, bench = _build_abc_v3_intraday_panel(
        conn, dates=trade_dates
    )
    f1_cands, _, _ = collect_abc_v3_skip0900_period_candidates(
        conn,
        dates=trade_dates,
        matcher=matches_w3_rv_hl_abc_v3_f1,
        frames=frames,
        trajectories=trajectories,
        meta=panel_meta,
        stock_maps=stock_maps,
        bench=bench,
    )
    f1_scored = apply_score_spec(f1_cands, spec_id="v0")
    f1_ordered = order_periods_for_fill(f1_scored, fill_mode="topk_score")
    f1_executed = select_executed_slot_periods(
        conn,
        close,
        trade_dates,
        f1_ordered,
        total_capital=cap_abc,
        n_slots=ABC_V3_SLOT_CHAMPION,
        cost_model=cost_model,
    )
    abc_periods = [
        {
            "entry_date": str(r["entry_date"]),
            "exit_date": str(r["exit_date"]),
            "stock_id": str(r["stock_id"]),
            "entry_px": r.get("entry_px"),
        }
        for r in f1_executed
    ]
    abc_pf = _portfolio_block(
        conn,
        close,
        trade_dates,
        abc_periods,
        total_capital=cap_abc,
        n_slots=ABC_V3_SLOT_CHAMPION,
        cost_model=cost_model,
    )

    # Normalize sleeve NAV to full-notional scale for combine_fixed_weight_nav
    scale_c = 1.0 / c18_weight if c18_weight > 0 else 0.0
    scale_a = 1.0 / abc_weight if abc_weight > 0 else 0.0
    c18_nav_scaled = [
        {"date": r["date"], "equity_ntd": round(float(r["equity_ntd"]) * scale_c, 2)}
        for r in (c18_pf.get("daily_nav") or [])
    ]
    abc_nav_scaled = [
        {"date": r["date"], "equity_ntd": round(float(r["equity_ntd"]) * scale_a, 2)}
        for r in (abc_pf.get("daily_nav") or [])
    ]

    grid = weight_grid or PHASE1_WEIGHT_GRID
    return build_phase1_payload(
        date_start=trade_dates[0],
        date_end=trade_dates[-1],
        n_trade_dates=len(trade_dates),
        notional=notional,
        c18_daily_nav=c18_nav_scaled,
        abc_daily_nav=abc_nav_scaled,
        c18_sleeve_metrics={**c18_pf, "capital_ntd": cap_c18},
        abc_sleeve_metrics={**abc_pf, "capital_ntd": cap_abc},
        trade_dates=trade_dates,
        weight_grid=grid,
        source="live_partitioned",
        runtime_s=round(time.perf_counter() - t0, 1),
    )


def render_c18acc_abc_dual_sleeve_phase1_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict", {})
    contract = payload.get("contract", {})
    baselines = payload.get("baselines", {})
    combos = payload.get("combos", {})
    ev = payload.get("event_level") or {}
    champ = ev.get("champion_pipeline") or {}
    abc_ev = ev.get("abc_f1") or {}
    abc_all = abc_ev.get("all_candidates") or {}
    abc_exec = abc_ev.get("executed_topk") or {}
    hold_days = abc_ev.get("hold_days") or 3
    default_key = str(payload.get("default_combo_key") or "")
    best_key = str(payload.get("best_combo_by_full_daily") or "")
    abc_pf = (
        (baselines.get("abc_f1_only") or {})
        .get("sleeves", {})
        .get("abc_f1")
        or (payload.get("strategies") or {}).get("abc-v3-f1-pullback", {}).get("portfolio")
        or {}
    )
    abc_util = abc_pf.get("avg_utilization_pct")

    def _row(block: dict, win: str = "FULL") -> str:
        m = (block.get("windows") or {}).get(win) or {}
        if m.get("error"):
            return f"| {win} | — | {m.get('error')} | — | — | — |"
        return (
            f"| {win} | {m.get('total_return_pct', '—')}% | {m.get('mean_daily_return_pct', '—')}% | "
            f"{m.get('cagr_pct', '—')}% | {m.get('sharpe_ratio', '—')} | {m.get('max_drawdown_pct', '—')}% |"
        )

    lines = [
        "# C18acc × ABC v3+f1 · Phase1 fixed-weight NAV",
        "",
        "> 獨立資金池 · 固定權重 · **不修改** strategy 規格。",
        "",
        f"- Span: **{payload.get('date_start')} → {payload.get('date_end')}** "
        f"({payload.get('n_trade_dates')} td)",
        f"- Notional: **{payload.get('notional_ntd'):,.0f} NTD**",
        f"- Default: **{contract.get('default_weights')}** · mode=`{contract.get('mode')}`",
        f"- Source: `{payload.get('source')}`",
        "",
        "> **兩層不可混讀**：ABC 採納依據是 **Event-level（事件層）** 每筆 3 天 α（**+2.845%**）；"
        "下方 Phase1 組合 NAV 用的是 **Portfolio NAV（組合層）** 5 槽資金受限模擬。"
        "ABC **5-slot portfolio val 未過** · 組合日均遠低於事件層實力。",
        "",
        "## ABC v3+f1 · 事件層（採納 edge · hold 3d）",
        "",
        "| 口徑 | n | 每筆 3d 報酬 % | 日均 % | 勝率 % | 備註 |",
        "|------|---|---------------|--------|--------|------|",
        f"| **Champion · {champ.get('label', '90d OOS')}** | **{champ.get('n', '—')}** | "
        f"**{champ.get('mean_ret_3d_net_pct', '—')}** | "
        f"{round(float(champ.get('mean_ret_3d_net_pct') or 0) / hold_days, 4) if champ.get('mean_ret_3d_net_pct') is not None else '—'} | "
        f"**{champ.get('win_rate_pct', '—')}** | `{champ.get('source', 'pipeline')}` |",
        f"| 本窗口 · 全候選 unconstrained | {abc_all.get('n', '—')} | "
        f"**{abc_all.get('mean_ret_net_pct', '—')}** | "
        f"{abc_all.get('mean_daily_ret_net_pct', '—')} | "
        f"{abc_all.get('win_rate_pct', '—')} | 無槽限制 |",
        f"| 本窗口 · 5 槽 top-K 成交 | {abc_exec.get('n', '—')} | "
        f"{abc_exec.get('mean_ret_net_pct', '—')} | "
        f"{abc_exec.get('mean_daily_ret_net_pct', '—')} | "
        f"{abc_exec.get('win_rate_pct', '—')} | 事件層 executed |",
        "",
        f"Phase1 ABC 袖使用 **5 槽 portfolio NAV**（資金利用率 {abc_util or '—'}%）· "
        "與 teaching deck 的 **+2.845% / 3d** 不是同一口徑。",
        "",
        "## Verdict（組合 NAV 門檻 · vs C18 portfolio）",
        "",
        f"- {v.get('summary', '')}",
        "",
        "## Baselines（單袖 100% · Portfolio NAV）",
        "",
    ]
    for label, key in (("C18acc only", "c18acc_only"), ("ABC v3+f1 only · 5-slot NAV", "abc_f1_only")):
        block = baselines.get(key, {})
        lines.append(f"### {label}")
        lines.append("")
        lines.append("| window | total ret | mean daily | CAGR | Sharpe | maxDD |")
        lines.append("|--------|----------:|-----------:|-----:|-------:|------:|")
        for win in ("FULL", "IS", "OOS_H1"):
            lines.append(_row(block, win))
        lines.append("")

    lines.extend(
        [
            "## Weight grid（partitioned）",
            "",
            "| combo | w_C18/w_ABC | FULL total | FULL daily | FULL CAGR | Sharpe | maxDD |",
            "|-------|-------------|----------:|-----------:|----------:|-------:|------:|",
        ]
    )
    for key, block in combos.items():
        m = (block.get("windows") or {}).get("FULL") or {}
        wc = block.get("c18_weight")
        wa = block.get("abc_weight")
        mark = []
        if key == default_key:
            mark.append("default")
        if key == best_key:
            mark.append("best_daily")
        tag = f" ({','.join(mark)})" if mark else ""
        lines.append(
            f"| {key}{tag} | {int((wc or 0)*100)}/{int((wa or 0)*100)} | "
            f"{m.get('total_return_pct', '—')}% | {m.get('mean_daily_return_pct', '—')}% | "
            f"{m.get('cagr_pct', '—')}% | {m.get('sharpe_ratio', '—')} | "
            f"{m.get('max_drawdown_pct', '—')}% |"
        )

    default_combo = combos.get(default_key, {})
    lines.extend(
        [
            "",
            f"- Daily return corr (sleeves): **{default_combo.get('daily_return_corr', '—')}**",
            "",
            "## Δ vs C18acc only（default combo · FULL）",
            "",
            f"- Δ mean daily: **{v.get('delta_full_mean_daily_pp', '—')} pp**",
            f"- Δ CAGR: **{v.get('delta_full_cagr_pp', '—')} pp**",
            f"- Δ total return: **{v.get('delta_full_total_return_pp', '—')} pp**",
            f"- Δ OOS H1 mean daily: **{v.get('delta_oos_h1_mean_daily_pp', '—')} pp**",
            "",
            "## Next",
            "",
            "- **NO_GO（組合層）** → 70/30 5 槽 NAV 未贏 C18 · 不代表事件層 +2.845% 無效",
            "- 若要做公平 dual-sleeve → Phase1b 需用 **event-level sleeve NAV**（非 5 槽 portfolio）",
            "- C18acc 單袖維持 · ABC 維持 event observe · 不 merge 篩選器",
            "",
        ]
    )
    return "\n".join(lines)
