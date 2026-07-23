"""富邦新店原始過濾器 × W20 Momentum × 3 槽逐層研究。

每一層只使用 IS 選參，下一層凍結前一層 IS winner；OOS 僅 replay。
排序契約是「流量/流動性/W20 gate → Top-N」，避免 gate 後不補位。
"""

from __future__ import annotations

from collections import defaultdict
import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any, Literal

from copytrade.branch_signals import (
    DEFAULT_TRADER_ID_FUBON_XINDIAN,
    iter_branch_amount_buy_signals,
    iter_branch_buy_signals,
)
from copytrade.signals import CopytradeSignal, group_signals_by_date
from rank_stats import max_drawdown_pct
from report_paths import REPORTS_RESEARCH
from research.backtest.amount_rrg_copytrade_funnel_study import (
    DEFAULT_OOS_CUT,
    DEFAULT_WINDOW_END,
    DEFAULT_WINDOW_START,
    _run_arm,
    build_funnel_feature_index,
    max_drawdown_ntd,
)
from research.backtest.copytrade_backtest import (
    DEFAULT_SIGNAL_CAPITAL_NTD,
    count_hold_trading_days,
    run_copytrade_backtest,
    select_executed_signal_days,
)
from research.backtest.yuanta_songjiang_copytrade_sweep import day_results_to_dicts

Metric = Literal["lots", "amount"]
ETF_CODE_PROXY = "FUBON_XD_FILTER"
FILE_STEM = "fubon_xindian_3slot_filter_study"
AMOUNT_FILE_STEM = "fubon_xindian_amount_3slot_optimization"
AMOUNT_ONESLOT_FILE_STEM = "fubon_xindian_amount_1slot_optimization"
STRATEGY_READINESS_FILE_STEM = "fubon_xindian_strategy_readiness"
SHARES_PER_LOT = 1_000.0
FIXED_TOTAL_CAPITAL_NTD = 30_000.0
DEFAULT_READINESS_AMOUNT_NTD = 50_000_000.0
FORWARD_START = "2026-07-18"
FORWARD_MINIMUM_COMPLETED_CYCLES = 12
FORWARD_MINIMUM_TRADING_DAYS = 90
FORWARD_COST_BPS = 60.0
FORWARD_MAX_DRAWDOWN_PCT = 20.0
FORWARD_MIN_WIN_RATE_PCT = 50.0


@dataclass(frozen=True)
class FilterConfig:
    metric: Metric = "lots"
    threshold: float = 500_000.0
    min_avg_volume_shares: float = 200_000.0
    w20_momentum_min: float | None = 101.0
    rs_ratio_min: float | None = None
    rrg_quadrant: str | None = None
    top_n: int = 1
    entry_lag: int = 1
    hold: int = 9
    n_slots: int = 3

    @property
    def id(self) -> str:
        threshold = (
            f"{self.threshold / 1e6:g}M"
            if self.metric == "amount"
            else f"{self.threshold / SHARES_PER_LOT:g}lots"
        )
        w20 = (
            "none"
            if self.w20_momentum_min is None
            else f"{self.w20_momentum_min:g}"
        )
        rs = (
            "none"
            if self.rs_ratio_min is None
            else f"{self.rs_ratio_min:g}"
        )
        quadrant = (
            f"_quad{self.rrg_quadrant}" if self.rrg_quadrant is not None else ""
        )
        return (
            f"{self.metric}_{threshold}_vol{self.min_avg_volume_shares/1e3:g}k_"
            f"w20{w20}_rs{rs}{quadrant}_top{self.top_n}_"
            f"L{self.entry_lag + 1}H{self.hold}_"
            f"S{self.n_slots}"
        )


def build_raw_xindian_grouped(
    conn: sqlite3.Connection,
    *,
    window_start: str,
    window_end: str,
    trader_id: str = DEFAULT_TRADER_ID_FUBON_XINDIAN,
) -> dict[str, list[CopytradeSignal]]:
    """Load the broad 50-lot mother set without liquidity/Top-N truncation."""
    signals = iter_branch_buy_signals(
        conn,
        trader_id,
        min_net_shares=50_000.0,
        top_n=10_000,
        window_start=window_start,
        window_end=window_end,
        min_avg_volume_shares=None,
    )
    return group_signals_by_date(signals)


def build_raw_xindian_amount_grouped(
    conn: sqlite3.Connection,
    *,
    window_start: str,
    window_end: str,
    min_net_amount_ntd: float = 30_000_000.0,
    trader_id: str = DEFAULT_TRADER_ID_FUBON_XINDIAN,
) -> dict[str, list[CopytradeSignal]]:
    """Load an amount-based mother set without an accidental share floor."""
    signals = iter_branch_amount_buy_signals(
        conn,
        trader_id,
        min_net_amount_ntd=min_net_amount_ntd,
        top_n=10_000,
        window_start=window_start,
        window_end=window_end,
        min_avg_volume_shares=None,
    )
    return group_signals_by_date(signals)


def build_config_grouped(
    raw: dict[str, list[CopytradeSignal]],
    features: dict[tuple[str, str], dict[str, Any]],
    config: FilterConfig,
) -> tuple[dict[str, list[CopytradeSignal]], dict[str, Any]]:
    """Apply filters, then rank and truncate Top-N."""
    out: dict[str, list[CopytradeSignal]] = {}
    n_raw = 0
    n_flow = 0
    n_liquid = 0
    n_quadrant = 0
    n_w20 = 0
    missing = 0
    for day, legs in raw.items():
        ranked: list[tuple[float, str, CopytradeSignal]] = []
        n_raw += len(legs)
        for leg in legs:
            sid = str(leg.stock_id)
            feat = features.get((day, sid))
            if feat is None:
                missing += 1
                continue
            close = feat.get("signal_close")
            avg_volume = feat.get("avg_volume20")
            momentum = feat.get("rs_momentum")
            if close is None or avg_volume is None:
                missing += 1
                continue
            net = float(leg.share_delta)
            score = net if config.metric == "lots" else net * float(close)
            if score < float(config.threshold):
                continue
            n_flow += 1
            if float(avg_volume) < float(config.min_avg_volume_shares):
                continue
            n_liquid += 1
            if config.rrg_quadrant is not None and str(
                feat.get("quadrant") or ""
            ).lower() != str(config.rrg_quadrant).lower():
                continue
            n_quadrant += 1
            if config.rs_ratio_min is not None and (
                feat.get("rs_ratio") is None
                or float(feat["rs_ratio"]) < float(config.rs_ratio_min)
            ):
                continue
            if config.w20_momentum_min is not None and (
                momentum is None
                or float(momentum) < float(config.w20_momentum_min)
            ):
                continue
            n_w20 += 1
            ranked.append((score, sid, leg))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        kept = [row[2] for row in ranked[: int(config.top_n)]]
        if kept:
            out[day] = kept
    n_top = sum(len(legs) for legs in out.values())
    return out, {
        "raw_legs": n_raw,
        "flow_pass_legs": n_flow,
        "liquidity_pass_legs": n_liquid,
        "quadrant_pass_legs": n_quadrant,
        "w20_pass_legs": n_w20,
        "topn_legs": n_top,
        "signal_days": len(out),
        "missing_features": missing,
        "end_to_end_funnel_pass_pct": (
            round(100.0 * n_top / n_raw, 2) if n_raw else None
        ),
    }


def run_config(
    conn: sqlite3.Connection,
    raw: dict[str, list[CopytradeSignal]],
    features: dict[tuple[str, str], dict[str, Any]],
    config: FilterConfig,
    *,
    window_start: str,
    window_end: str,
    oos_cut: str,
    capital_ntd: float | None = None,
) -> dict[str, Any]:
    grouped, funnel = build_config_grouped(raw, features, config)
    slot_capital = (
        float(capital_ntd)
        if capital_ntd is not None
        else FIXED_TOTAL_CAPITAL_NTD / max(int(config.n_slots), 1)
    )
    metrics = _run_arm(
        conn,
        grouped,
        label=config.id,
        n_slots=config.n_slots,
        capital_ntd=slot_capital,
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
        entry_lag=config.entry_lag,
        hold=config.hold,
        etf_code_proxy=ETF_CODE_PROXY,
    )
    return {
        "config": asdict(config),
        "config_id": config.id,
        "funnel": funnel,
        "metrics": metrics,
    }


def _select_is_balanced(
    arms: list[dict[str, Any]],
    baseline: dict[str, Any],
) -> dict[str, Any] | None:
    """Max IS total alpha, while retaining cycles and per-cycle quality."""
    base = baseline["metrics"]["is"]
    base_cycles = int(base.get("n_cycles") or 0)
    base_apc = float(base.get("alpha_per_cycle") or 0.0)
    viable = [
        arm
        for arm in arms
        if int(arm["metrics"]["is"].get("n_cycles") or 0)
        >= max(8, int(base_cycles * 0.70))
        and float(arm["metrics"]["is"].get("alpha_per_cycle") or -1e18)
        >= base_apc
    ]
    if not viable:
        return None
    return max(
        viable,
        key=lambda arm: (
            float(arm["metrics"]["is"].get("alpha_ntd") or -1e18),
            float(arm["metrics"]["is"].get("pnl_ntd") or -1e18),
            int(arm["metrics"]["is"].get("n_cycles") or 0),
        ),
    )


def _select_is_period_return(
    arms: list[dict[str, Any]],
    baseline: dict[str, Any],
) -> dict[str, Any] | None:
    """1-slot objective: max IS period return with sample/quality floors."""
    base = baseline["metrics"]["is"]
    base_cycles = int(base.get("n_cycles") or 0)
    base_apc = float(base.get("alpha_per_cycle") or 0.0)
    base_dd = float(base.get("max_drawdown_ntd") or 0.0)
    viable = []
    for arm in arms:
        ism = arm["metrics"]["is"]
        cycles = int(ism.get("n_cycles") or 0)
        apc = float(ism.get("alpha_per_cycle") or -1e18)
        dd = float(ism.get("max_drawdown_ntd") or 0.0)
        if cycles < max(10, int(base_cycles * 0.70)):
            continue
        if apc < base_apc * 0.90:
            continue
        # Drawdown magnitude not >1.5x worse than baseline
        if base_dd < 0 and dd < base_dd * 1.5:
            continue
        viable.append(arm)
    if not viable:
        return None
    return max(
        viable,
        key=lambda arm: (
            float(arm["metrics"]["is"].get("period_return_pct") or -1e18),
            float(arm["metrics"]["is"].get("alpha_ntd") or -1e18),
            float(arm["metrics"]["is"].get("pnl_ntd") or -1e18),
        ),
    )


def _stage_oos_gate(
    selected: dict[str, Any] | None,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    if selected is None:
        return {
            "status": "no_is_candidate",
            "passed": False,
            "reason": "IS 無同時保留樣本與 α/筆的參數",
        }
    arm = selected["metrics"]["oos"]
    base = baseline["metrics"]["oos"]
    arm_cycles = int(arm.get("n_cycles") or 0)
    base_cycles = int(base.get("n_cycles") or 0)
    arm_alpha = float(arm.get("alpha_ntd") or 0.0)
    base_alpha = float(base.get("alpha_ntd") or 0.0)
    arm_apc = float(arm.get("alpha_per_cycle") or 0.0)
    base_apc = float(base.get("alpha_per_cycle") or 0.0)
    arm_month = arm.get("positive_month_pct")
    base_month = base.get("positive_month_pct")
    retain_cycles = arm_cycles >= max(8, int(base_cycles * 0.70))
    alpha_ok = base_alpha <= 0 or arm_alpha >= base_alpha * 0.95
    apc_ok = base_apc <= 0 or arm_apc >= base_apc * 0.95
    improves = (
        (base_alpha <= 0 or arm_alpha >= base_alpha * 1.05)
        or (base_apc <= 0 or arm_apc >= base_apc * 1.05)
    )
    month_ok = (
        arm_month is not None
        and base_month is not None
        and float(arm_month) >= float(base_month) - 10.0
    )
    checks = {
        "retain_oos_cycles_ge_70pct": retain_cycles,
        "total_alpha_not_worse_5pct": alpha_ok,
        "alpha_per_cycle_not_worse_5pct": apc_ok,
        "alpha_or_quality_improves_5pct": improves,
        "positive_month_not_worse_10pt": month_ok,
    }
    passed = all(checks.values())
    unchanged = (
        arm_cycles == base_cycles
        and abs(arm_alpha - base_alpha) < 0.01
        and abs(arm_apc - base_apc) < 0.01
        and abs(
            float(arm.get("pnl_ntd") or 0.0)
            - float(base.get("pnl_ntd") or 0.0)
        )
        < 0.01
    )
    return {
        "status": "passed" if passed else ("redundant" if unchanged else "rejected"),
        "passed": passed,
        "checks": checks,
        "delta_oos_cycles": arm_cycles - base_cycles,
        "delta_oos_alpha_ntd": round(arm_alpha - base_alpha, 2),
        "delta_oos_alpha_per_cycle": round(arm_apc - base_apc, 2),
        "delta_oos_pnl_ntd": round(
            float(arm.get("pnl_ntd") or 0.0)
            - float(base.get("pnl_ntd") or 0.0),
            2,
        ),
    }


def _stage_oos_gate_return(
    selected: dict[str, Any] | None,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """1-slot OOS gate emphasizing period return under fixed capital."""
    if selected is None:
        return {
            "status": "no_is_candidate",
            "passed": False,
            "reason": "IS 無同時保留樣本與報酬率的參數",
        }
    arm = selected["metrics"]["oos"]
    base = baseline["metrics"]["oos"]
    arm_cycles = int(arm.get("n_cycles") or 0)
    base_cycles = int(base.get("n_cycles") or 0)
    arm_ret = float(arm.get("period_return_pct") or 0.0)
    base_ret = float(base.get("period_return_pct") or 0.0)
    arm_apc = float(arm.get("alpha_per_cycle") or 0.0)
    base_apc = float(base.get("alpha_per_cycle") or 0.0)
    arm_pnl = float(arm.get("pnl_ntd") or 0.0)
    base_pnl = float(base.get("pnl_ntd") or 0.0)
    arm_month = arm.get("positive_month_pct")
    base_month = base.get("positive_month_pct")
    arm_dd = float(arm.get("max_drawdown_ntd") or 0.0)
    base_dd = float(base.get("max_drawdown_ntd") or 0.0)
    retain_cycles = arm_cycles >= max(8, int(base_cycles * 0.70))
    ret_ok = base_ret <= 0 or arm_ret >= base_ret * 0.95
    apc_ok = base_apc <= 0 or arm_apc >= base_apc * 0.90
    improves = (
        (base_ret <= 0 or arm_ret >= base_ret * 1.05)
        or (base_pnl <= 0 or arm_pnl >= base_pnl * 1.05)
    )
    month_ok = (
        arm_month is not None
        and base_month is not None
        and float(arm_month) >= float(base_month) - 10.0
    )
    dd_ok = not (base_dd < 0 and arm_dd < base_dd * 1.5)
    checks = {
        "retain_oos_cycles_ge_70pct": retain_cycles,
        "period_return_not_worse_5pct": ret_ok,
        "alpha_per_cycle_not_worse_10pct": apc_ok,
        "return_or_pnl_improves_5pct": improves,
        "positive_month_not_worse_10pt": month_ok,
        "drawdown_not_1p5x_worse": dd_ok,
    }
    passed = all(checks.values())
    unchanged = (
        arm_cycles == base_cycles
        and abs(arm_ret - base_ret) < 0.01
        and abs(arm_pnl - base_pnl) < 0.01
    )
    return {
        "status": "passed" if passed else ("redundant" if unchanged else "rejected"),
        "passed": passed,
        "checks": checks,
        "delta_oos_cycles": arm_cycles - base_cycles,
        "delta_oos_period_return_pct": round(arm_ret - base_ret, 4),
        "delta_oos_pnl_ntd": round(arm_pnl - base_pnl, 2),
        "delta_oos_alpha_per_cycle": round(arm_apc - base_apc, 2),
    }


def _run_stage(
    conn: sqlite3.Connection,
    raw: dict[str, list[CopytradeSignal]],
    features: dict[tuple[str, str], dict[str, Any]],
    *,
    stage: str,
    baseline: dict[str, Any],
    configs: list[FilterConfig],
    window_start: str,
    window_end: str,
    oos_cut: str,
    capital_ntd: float | None = None,
    select_fn=_select_is_balanced,
    gate_fn=_stage_oos_gate,
) -> dict[str, Any]:
    arms = [
        run_config(
            conn,
            raw,
            features,
            config,
            window_start=window_start,
            window_end=window_end,
            oos_cut=oos_cut,
            capital_ntd=capital_ntd,
        )
        for config in configs
    ]
    selected = select_fn(arms, baseline)
    return {
        "stage": stage,
        "baseline": baseline,
        "arms": arms,
        "selected_on_is": selected,
        "oos_gate": gate_fn(selected, baseline),
    }


def run_fubon_xindian_filter_study(
    conn: sqlite3.Connection,
    *,
    window_start: str = DEFAULT_WINDOW_START,
    window_end: str = DEFAULT_WINDOW_END,
    oos_cut: str = DEFAULT_OOS_CUT,
    trader_id: str = DEFAULT_TRADER_ID_FUBON_XINDIAN,
) -> dict[str, Any]:
    raw = build_raw_xindian_grouped(
        conn,
        window_start=window_start,
        window_end=window_end,
        trader_id=trader_id,
    )
    features = build_funnel_feature_index(conn, raw)

    original = FilterConfig(w20_momentum_min=None, n_slots=3)
    original_run = run_config(
        conn,
        raw,
        features,
        original,
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
    )
    w20_base = replace(original, w20_momentum_min=101.0)
    w20_run = run_config(
        conn,
        raw,
        features,
        w20_base,
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
    )
    w20_gate = _stage_oos_gate(w20_run, original_run)

    flow_configs = [
        replace(w20_base, metric="lots", threshold=lots * SHARES_PER_LOT)
        for lots in (100, 200, 300, 400, 500, 650, 800, 1_000)
    ] + [
        replace(w20_base, metric="amount", threshold=amount * 1_000_000.0)
        for amount in (50, 65, 80, 100, 120, 150)
    ]
    flow = _run_stage(
        conn,
        raw,
        features,
        stage="flow_metric_threshold",
        baseline=w20_run,
        configs=flow_configs,
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
    )
    flow_winner = flow["selected_on_is"] or w20_run
    flow_cfg = FilterConfig(**flow_winner["config"])

    volume_configs = [
        replace(flow_cfg, min_avg_volume_shares=float(volume))
        for volume in (0, 100_000, 200_000, 300_000, 500_000, 800_000, 1_000_000)
    ]
    volume = _run_stage(
        conn,
        raw,
        features,
        stage="liquidity_floor",
        baseline=flow_winner,
        configs=volume_configs,
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
    )
    volume_winner = volume["selected_on_is"] or flow_winner
    volume_cfg = FilterConfig(**volume_winner["config"])

    topn_configs = [
        replace(volume_cfg, top_n=top_n) for top_n in (1, 2, 3, 5)
    ]
    topn = _run_stage(
        conn,
        raw,
        features,
        stage="top_n_after_gates",
        baseline=volume_winner,
        configs=topn_configs,
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
    )
    topn_winner = topn["selected_on_is"] or volume_winner
    topn_cfg = FilterConfig(**topn_winner["config"])

    timing_configs = [
        replace(topn_cfg, entry_lag=lag, hold=hold)
        for lag in (0, 1, 2)
        for hold in (7, 9, 12, 15, 18)
    ]
    timing = _run_stage(
        conn,
        raw,
        features,
        stage="entry_hold",
        baseline=topn_winner,
        configs=timing_configs,
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
    )
    final = timing["selected_on_is"] or topn_winner
    final_vs_original = _stage_oos_gate(final, original_run)
    amount_reference_config = FilterConfig(
        metric="amount",
        threshold=80_000_000.0,
        min_avg_volume_shares=200_000.0,
        w20_momentum_min=101.0,
        top_n=1,
        entry_lag=1,
        hold=12,
        n_slots=3,
    )
    amount_reference = run_config(
        conn,
        raw,
        features,
        amount_reference_config,
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
    )
    final_vs_amount_reference = _stage_oos_gate(final, amount_reference)

    return {
        "schema": "fubon_xindian_3slot_filter_study-v1",
        "contract": {
            "window_start": window_start,
            "window_end": window_end,
            "oos_cut": oos_cut,
            "trader_id": trader_id,
            "slots": 3,
            "capital_ntd_per_slot": 10_000,
            "cost_bps": 0.0,
            "selection": "sequential IS-only; OOS replay",
            "filter_order": "flow → liquidity → W20 → Top-N → slots",
            "raw_floor": "50 lots, no liquidity, no Top-N truncation",
        },
        "raw": {
            "signal_days": len(raw),
            "legs": sum(len(legs) for legs in raw.values()),
            "feature_rows": len(features),
        },
        "original_500lots_top1_l2h9_3slot": original_run,
        "original_plus_w20_101": w20_run,
        "w20_oos_gate": w20_gate,
        "stages": [flow, volume, topn, timing],
        "final_is_selected": final,
        "final_oos_gate_vs_original": final_vs_original,
        "amount_80m_w20_101_l2h12_reference": amount_reference,
        "final_oos_gate_vs_amount_reference": final_vs_amount_reference,
        "hypotheses": {
            "H_FX_W20_ON_ORIGINAL": {
                "status": w20_gate["status"],
                "gate": w20_gate,
            },
            **{
                f"H_FX_{stage['stage'].upper()}": {
                    "status": stage["oos_gate"]["status"],
                    "gate": stage["oos_gate"],
                    "selected_config": (
                        stage["selected_on_is"]["config"]
                        if stage["selected_on_is"]
                        else None
                    ),
                }
                for stage in (flow, volume, topn, timing)
            },
            "H_FX_FINAL_REPLACES_ORIGINAL": {
                "status": final_vs_original["status"],
                "gate": final_vs_original,
            },
            "H_FX_ORIGINAL_FILTERS_BEAT_AMOUNT_REFERENCE": {
                "status": final_vs_amount_reference["status"],
                "gate": final_vs_amount_reference,
            },
        },
    }


def run_fubon_xindian_amount_optimization(
    conn: sqlite3.Connection,
    *,
    window_start: str = DEFAULT_WINDOW_START,
    window_end: str = DEFAULT_WINDOW_END,
    oos_cut: str = DEFAULT_OOS_CUT,
    trader_id: str = DEFAULT_TRADER_ID_FUBON_XINDIAN,
) -> dict[str, Any]:
    """Sequential amount-only optimization; 80M is a legacy reference."""
    raw = build_raw_xindian_grouped(
        conn,
        window_start=window_start,
        window_end=window_end,
        trader_id=trader_id,
    )
    features = build_funnel_feature_index(conn, raw)
    baseline_config = FilterConfig(
        metric="amount",
        threshold=80_000_000.0,
        min_avg_volume_shares=200_000.0,
        w20_momentum_min=101.0,
        top_n=1,
        entry_lag=1,
        hold=12,
        n_slots=3,
    )
    baseline = run_config(
        conn,
        raw,
        features,
        baseline_config,
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
    )

    amount_threshold = _run_stage(
        conn,
        raw,
        features,
        stage="amount_threshold",
        baseline=baseline,
        configs=[
            replace(baseline_config, threshold=amount * 1_000_000.0)
            for amount in (50, 65, 80, 90, 100, 110, 120, 150)
        ],
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
    )
    amount_winner = amount_threshold["selected_on_is"] or baseline
    amount_config = FilterConfig(**amount_winner["config"])

    w20 = _run_stage(
        conn,
        raw,
        features,
        stage="w20_momentum_threshold",
        baseline=amount_winner,
        configs=[
            replace(amount_config, w20_momentum_min=float(threshold))
            for threshold in (99, 100, 101, 102, 103, 104)
        ],
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
    )
    w20_winner = w20["selected_on_is"] or amount_winner
    w20_config = FilterConfig(**w20_winner["config"])

    volume = _run_stage(
        conn,
        raw,
        features,
        stage="liquidity_floor",
        baseline=w20_winner,
        configs=[
            replace(w20_config, min_avg_volume_shares=float(volume))
            for volume in (0, 100_000, 200_000, 500_000, 1_000_000)
        ],
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
    )
    volume_winner = volume["selected_on_is"] or w20_winner
    volume_config = FilterConfig(**volume_winner["config"])

    topn = _run_stage(
        conn,
        raw,
        features,
        stage="top_n_after_gates",
        baseline=volume_winner,
        configs=[replace(volume_config, top_n=top_n) for top_n in (1, 2, 3, 5)],
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
    )
    topn_winner = topn["selected_on_is"] or volume_winner
    topn_config = FilterConfig(**topn_winner["config"])

    timing = _run_stage(
        conn,
        raw,
        features,
        stage="entry_hold",
        baseline=topn_winner,
        configs=[
            replace(topn_config, entry_lag=lag, hold=hold)
            for lag in (0, 1, 2)
            for hold in (9, 12, 15, 18)
        ],
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
    )
    final = timing["selected_on_is"] or topn_winner
    final_gate = _stage_oos_gate(final, baseline)
    stages = [amount_threshold, w20, volume, topn, timing]
    gate_surviving = amount_winner
    gate_surviving_gate = _stage_oos_gate(gate_surviving, baseline)

    return {
        "schema": "fubon_xindian_amount_3slot_optimization-v1",
        "contract": {
            "window_start": window_start,
            "window_end": window_end,
            "oos_cut": oos_cut,
            "trader_id": trader_id,
            "slots": 3,
            "capital_ntd_per_slot": 10_000,
            "cost_bps": 0.0,
            "selection": "sequential IS-only; viewed OOS is robustness replay",
            "filter_order": "amount → liquidity → W20 → Top-N → slots",
            "legacy_reference": "80M × W20≥101 × Top1 × L2H12",
        },
        "raw": {
            "signal_days": len(raw),
            "legs": sum(len(legs) for legs in raw.values()),
            "feature_rows": len(features),
        },
        "legacy_80m_reference": baseline,
        "stages": stages,
        "final_is_selected": final,
        "final_oos_gate_vs_legacy_80m": final_gate,
        "gate_surviving_candidate": gate_surviving,
        "gate_surviving_oos_gate_vs_legacy_80m": gate_surviving_gate,
        "hypotheses": {
            **{
                f"H_FX_AMOUNT_{stage['stage'].upper()}": {
                    "status": stage["oos_gate"]["status"],
                    "gate": stage["oos_gate"],
                    "selected_config": (
                        stage["selected_on_is"]["config"]
                        if stage["selected_on_is"]
                        else None
                    ),
                }
                for stage in stages
            },
            "H_FX_AMOUNT_FINAL_BEATS_LEGACY_80M": {
                "status": final_gate["status"],
                "gate": final_gate,
                "note": "IS-only pipeline includes Top2/L1H12 layers rejected individually",
            },
            "H_FX_AMOUNT_GATE_SURVIVOR_BEATS_LEGACY_80M": {
                "status": gate_surviving_gate["status"],
                "gate": gate_surviving_gate,
                "selected_config": gate_surviving["config"],
            },
        },
    }


def run_fubon_xindian_amount_oneslot_optimization(
    conn: sqlite3.Connection,
    *,
    window_start: str = DEFAULT_WINDOW_START,
    window_end: str = DEFAULT_WINDOW_END,
    oos_cut: str = DEFAULT_OOS_CUT,
    trader_id: str = DEFAULT_TRADER_ID_FUBON_XINDIAN,
    total_capital_ntd: float = FIXED_TOTAL_CAPITAL_NTD,
) -> dict[str, Any]:
    """1-slot amount optimization under fixed total capital.

    Objective: maximize IS period return; OOS is robustness replay only.
    Fair money comparison: 1×total vs 3×(total/3).
    """
    raw = build_raw_xindian_grouped(
        conn,
        window_start=window_start,
        window_end=window_end,
        trader_id=trader_id,
    )
    features = build_funnel_feature_index(conn, raw)
    capital_1 = float(total_capital_ntd)
    capital_3 = float(total_capital_ntd) / 3.0

    baseline_config = FilterConfig(
        metric="amount",
        threshold=80_000_000.0,
        min_avg_volume_shares=200_000.0,
        w20_momentum_min=None,
        rs_ratio_min=100.0,
        top_n=1,
        entry_lag=1,
        hold=12,
        n_slots=1,
    )
    baseline = run_config(
        conn,
        raw,
        features,
        baseline_config,
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
        capital_ntd=capital_1,
    )
    # Same filters as legacy 3-slot money peer (fixed total capital)
    peer_3slot = run_config(
        conn,
        raw,
        features,
        replace(baseline_config, n_slots=3),
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
        capital_ntd=capital_3,
    )

    amount_threshold = _run_stage(
        conn,
        raw,
        features,
        stage="amount_threshold",
        baseline=baseline,
        configs=[
            replace(baseline_config, threshold=amount * 1_000_000.0)
            for amount in (50, 65, 80, 90, 100, 110, 120, 150)
        ],
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
        capital_ntd=capital_1,
        select_fn=_select_is_period_return,
        gate_fn=_stage_oos_gate_return,
    )
    amount_freeze = (
        amount_threshold["selected_on_is"]
        if amount_threshold["oos_gate"].get("status") in {"passed", "redundant"}
        and amount_threshold["selected_on_is"]
        else baseline
    )
    amount_config = FilterConfig(**amount_freeze["config"])

    # Orthogonal gate retune: keep rs_ratio, optionally add W20; or tighten rs
    gate_configs = [
        replace(amount_config, rs_ratio_min=rs, w20_momentum_min=None)
        for rs in (100.0, 101.0, 102.0, 103.0)
    ] + [
        replace(amount_config, rs_ratio_min=100.0, w20_momentum_min=float(w20))
        for w20 in (99.0, 100.0, 101.0, 102.0)
    ] + [
        replace(amount_config, rs_ratio_min=None, w20_momentum_min=float(w20))
        for w20 in (100.0, 101.0, 102.0)
    ]
    rrg_gate = _run_stage(
        conn,
        raw,
        features,
        stage="rrg_gate",
        baseline=amount_freeze,
        configs=gate_configs,
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
        capital_ntd=capital_1,
        select_fn=_select_is_period_return,
        gate_fn=_stage_oos_gate_return,
    )
    rrg_freeze = (
        rrg_gate["selected_on_is"]
        if rrg_gate["oos_gate"].get("status") in {"passed", "redundant"}
        and rrg_gate["selected_on_is"]
        else amount_freeze
    )
    rrg_config = FilterConfig(**rrg_freeze["config"])

    timing = _run_stage(
        conn,
        raw,
        features,
        stage="entry_hold",
        baseline=rrg_freeze,
        configs=[
            replace(rrg_config, entry_lag=lag, hold=hold)
            for lag in (0, 1, 2)
            for hold in (9, 12, 15, 18, 20)
        ],
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
        capital_ntd=capital_1,
        select_fn=_select_is_period_return,
        gate_fn=_stage_oos_gate_return,
    )
    stages = [amount_threshold, rrg_gate, timing]
    surviving = (
        timing["selected_on_is"]
        if timing["oos_gate"].get("status") in {"passed", "redundant"}
        and timing["selected_on_is"]
        else rrg_freeze
    )

    final_is = timing["selected_on_is"] or rrg_freeze
    final_gate = _stage_oos_gate_return(final_is, baseline)
    surviving_gate = _stage_oos_gate_return(surviving, baseline)
    surviving_vs_3slot = _stage_oos_gate_return(surviving, peer_3slot)

    return {
        "schema": "fubon_xindian_amount_1slot_optimization-v1",
        "contract": {
            "window_start": window_start,
            "window_end": window_end,
            "oos_cut": oos_cut,
            "trader_id": trader_id,
            "slots": 1,
            "total_capital_ntd": total_capital_ntd,
            "capital_ntd_per_slot": capital_1,
            "cost_bps": 0.0,
            "selection": (
                "sequential IS-only max period_return; "
                "viewed OOS is robustness replay"
            ),
            "filter_order": "amount → RRG gate → L/H",
            "legacy_reference": "80M × RS-ratio≥100 × Top1 × L2H12 × 1槽",
            "fair_peer": "same filters × 3槽 × capital=total/3",
        },
        "raw": {
            "signal_days": len(raw),
            "legs": sum(len(legs) for legs in raw.values()),
            "feature_rows": len(features),
        },
        "legacy_80m_1slot_reference": baseline,
        "fair_3slot_peer": peer_3slot,
        "stages": stages,
        "final_is_selected": final_is,
        "final_oos_gate_vs_legacy": final_gate,
        "gate_surviving_candidate": surviving,
        "gate_surviving_oos_gate_vs_legacy": surviving_gate,
        "gate_surviving_vs_fair_3slot": surviving_vs_3slot,
        "hypotheses": {
            **{
                f"H_FX_1SLOT_{stage['stage'].upper()}": {
                    "status": stage["oos_gate"]["status"],
                    "gate": stage["oos_gate"],
                    "selected_config": (
                        stage["selected_on_is"]["config"]
                        if stage["selected_on_is"]
                        else None
                    ),
                }
                for stage in stages
            },
            "H_FX_1SLOT_SURVIVOR_BEATS_LEGACY": {
                "status": surviving_gate["status"],
                "gate": surviving_gate,
                "selected_config": surviving["config"],
            },
            "H_FX_1SLOT_BEATS_FAIR_3SLOT_SAME_CAPITAL": {
                "status": surviving_vs_3slot["status"],
                "gate": surviving_vs_3slot,
                "note": "same total capital 30k; return% comparison",
            },
        },
    }


def _trimmed_mean_pct(values: list[float], *, trim_each: int = 1) -> float | None:
    if trim_each < 0:
        raise ValueError("trim_each must be >= 0")
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if trim_each == 0:
        return mean(ordered)
    if len(ordered) <= trim_each * 2:
        return None
    return mean(ordered[trim_each:-trim_each])


def _readiness_slice(
    conn: sqlite3.Connection,
    day_dicts: list[dict[str, Any]],
    *,
    start: str,
    end: str,
    n_slots: int = 1,
) -> dict[str, Any]:
    complete = [
        row
        for row in day_dicts
        if row.get("status") == "complete"
        and start <= str(row.get("signal_date") or "") < end
    ]
    executed, slot_meta = select_executed_signal_days(complete, n_slots=n_slots)
    returns = [float(row["return_pct"]) for row in executed]
    benchmarks = [float(row.get("bench_return_pct") or 0.0) for row in executed]
    excess = [ret - bench for ret, bench in zip(returns, benchmarks)]
    pnls = [float(row.get("pnl_ntd") or 0.0) for row in executed]
    locked_days = sum(
        count_hold_trading_days(
            conn,
            str(row["entry_date"]),
            str(row["exit_date"]),
        )
        for row in executed
        if row.get("entry_date") and row.get("exit_date")
    )
    monthly_pnl: dict[str, float] = defaultdict(float)
    for row in executed:
        monthly_pnl[str(row.get("exit_date") or "")[:7]] += float(
            row.get("pnl_ntd") or 0.0
        )
    trimmed_mean = _trimmed_mean_pct(returns)
    return {
        "start": start,
        "end_exclusive": end,
        "n_signals": len(complete),
        "n_cycles": len(executed),
        "capture_pct": slot_meta.get("signal_capture_pct"),
        "mean_return_pct": round(mean(returns), 4) if returns else None,
        "trimmed_mean_return_pct": (
            round(trimmed_mean, 4) if trimmed_mean is not None else None
        ),
        "median_return_pct": round(median(returns), 4) if returns else None,
        "win_rate_pct": (
            round(100.0 * sum(value > 0 for value in returns) / len(returns), 2)
            if returns
            else None
        ),
        "mean_benchmark_pct": round(mean(benchmarks), 4) if benchmarks else None,
        "mean_excess_pct": round(mean(excess), 4) if excess else None,
        "median_excess_pct": round(median(excess), 4) if excess else None,
        "max_drawdown_pct": max_drawdown_pct(returns),
        "max_drawdown_ntd": max_drawdown_ntd(pnls),
        "positive_month_pct": (
            round(
                100.0
                * sum(value > 0 for value in monthly_pnl.values())
                / len(monthly_pnl),
                2,
            )
            if monthly_pnl
            else None
        ),
        "locked_days": locked_days,
        "trimmed_return_per_locked_day_pct": (
            round(trimmed_mean * len(executed) / locked_days, 4)
            if locked_days and trimmed_mean is not None
            else None
        ),
    }


def _run_readiness_config(
    conn: sqlite3.Connection,
    raw: dict[str, list[CopytradeSignal]],
    features: dict[tuple[str, str], dict[str, Any]],
    config: FilterConfig,
    *,
    window_start: str,
    window_end: str,
    oos_cut: str,
    cost_bps: float,
) -> dict[str, Any]:
    grouped, funnel = build_config_grouped(raw, features, config)
    result = run_copytrade_backtest(
        conn,
        ETF_CODE_PROXY,
        strategy_id=f"L{config.entry_lag + 1}H{config.hold}",
        strategy_label=config.id,
        entry_lag_days=config.entry_lag,
        hold_trading_days=config.hold,
        entry_price_mode="open",
        capital_ntd=DEFAULT_SIGNAL_CAPITAL_NTD,
        cost_bps=cost_bps,
        window_start=window_start,
        window_end=window_end,
        grouped=grouped,
    )
    day_dicts = day_results_to_dicts(result)
    end_exclusive = (date.fromisoformat(window_end) + timedelta(days=1)).isoformat()
    return {
        "config": asdict(config),
        "config_id": config.id,
        "cost_bps": cost_bps,
        "funnel": funnel,
        "metrics": {
            "full": _readiness_slice(
                conn, day_dicts, start=window_start, end=end_exclusive
            ),
            "is": _readiness_slice(
                conn, day_dicts, start=window_start, end=oos_cut
            ),
            "oos": _readiness_slice(
                conn, day_dicts, start=oos_cut, end=end_exclusive
            ),
        },
        "_day_dicts": day_dicts,
    }


def evaluate_strategy_readiness_gates(
    *,
    cost_60: dict[str, Any],
    neighborhood: list[dict[str, Any]],
    folds: list[dict[str, Any]],
    untouched_forward_available: bool,
) -> dict[str, Any]:
    full = cost_60["metrics"]["full"]
    oos = cost_60["metrics"]["oos"]
    stable_neighbors = sum(
        int(
            float(arm["metrics"]["is"].get("median_return_pct") or -1e18) > 0
            and float(arm["metrics"]["oos"].get("median_return_pct") or -1e18) > 0
            and float(arm["metrics"]["oos"].get("mean_excess_pct") or -1e18) > 0
        )
        for arm in neighborhood
    )
    stable_folds = sum(
        int(
            int(fold.get("n_cycles") or 0) >= 5
            and float(fold.get("median_return_pct") or -1e18) > 0
            and float(fold.get("mean_excess_pct") or -1e18) > 0
        )
        for fold in folds
    )
    checks = {
        "full_cycles_ge_40": int(full.get("n_cycles") or 0) >= 40,
        "historical_oos_cycles_ge_12": int(oos.get("n_cycles") or 0) >= 12,
        "cost60_full_median_positive": float(
            full.get("median_return_pct") or -1e18
        )
        > 0,
        "cost60_oos_median_positive": float(
            oos.get("median_return_pct") or -1e18
        )
        > 0,
        "cost60_full_mean_excess_positive": float(
            full.get("mean_excess_pct") or -1e18
        )
        > 0,
        "cost60_oos_mean_excess_positive": float(
            oos.get("mean_excess_pct") or -1e18
        )
        > 0,
        "cost60_max_drawdown_le_25pct": float(
            full.get("max_drawdown_pct") or 1e18
        )
        <= 25.0,
        "stable_parameter_neighbors_ge_4_of_9": stable_neighbors >= 4,
        "stable_time_folds_ge_3_of_4": stable_folds >= 3,
        "untouched_forward_available": untouched_forward_available,
    }
    historical_checks = {
        key: value for key, value in checks.items() if key != "untouched_forward_available"
    }
    historical_passed = all(historical_checks.values())
    return {
        "checks": checks,
        "stable_parameter_neighbors": stable_neighbors,
        "stable_time_folds": stable_folds,
        "historical_passed": historical_passed,
        "adoption_passed": historical_passed and untouched_forward_available,
        "decision": (
            "adopt_strategy"
            if historical_passed and untouched_forward_available
            else (
                "paper_forward_required"
                if historical_passed
                else "historical_gates_failed"
            )
        ),
    }


def _count_trading_days(
    conn: sqlite3.Connection,
    *,
    start: str,
    end_inclusive: str,
) -> int:
    if end_inclusive < start:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT trade_date)
        FROM stock_daily_bars
        WHERE source = 'finmind'
          AND trade_date >= ?
          AND trade_date <= ?
        """,
        (start, end_inclusive),
    ).fetchone()
    return int(row[0] or 0)


def evaluate_forward_gate(
    *,
    trading_days: int,
    forward_metrics: dict[str, Any] | None,
    minimum_completed_cycles: int = FORWARD_MINIMUM_COMPLETED_CYCLES,
    minimum_trading_days: int = FORWARD_MINIMUM_TRADING_DAYS,
    max_drawdown_pct_limit: float = FORWARD_MAX_DRAWDOWN_PCT,
    min_win_rate_pct: float = FORWARD_MIN_WIN_RATE_PCT,
) -> dict[str, Any]:
    """Objective untouched-forward gate · never manually force-pass."""
    metrics = forward_metrics or {}
    n_cycles = int(metrics.get("n_cycles") or 0)
    median_ret = metrics.get("median_return_pct")
    mean_excess = metrics.get("mean_excess_pct")
    win_rate = metrics.get("win_rate_pct")
    max_dd = metrics.get("max_drawdown_pct")
    checks = {
        "trading_days_ge_minimum": trading_days >= minimum_trading_days,
        "completed_cycles_ge_minimum": n_cycles >= minimum_completed_cycles,
        "median_return_pct_gt_0": (
            median_ret is not None and float(median_ret) > 0.0
        ),
        "mean_excess_pct_gt_0": (
            mean_excess is not None and float(mean_excess) > 0.0
        ),
        "win_rate_pct_ge_50": (
            win_rate is not None and float(win_rate) >= min_win_rate_pct
        ),
        "max_drawdown_pct_le_20": (
            max_dd is not None and float(max_dd) <= max_drawdown_pct_limit
        ),
        "config_unchanged": True,
    }
    return {
        "forward_start": FORWARD_START,
        "minimum_completed_cycles": minimum_completed_cycles,
        "minimum_trading_days": minimum_trading_days,
        "cost_bps": FORWARD_COST_BPS,
        "trading_days": trading_days,
        "metrics": metrics,
        "checks": checks,
        "passed": all(checks.values()),
    }


def run_fubon_xindian_strategy_readiness(
    conn: sqlite3.Connection,
    *,
    window_start: str = DEFAULT_WINDOW_START,
    window_end: str = DEFAULT_WINDOW_END,
    oos_cut: str = DEFAULT_OOS_CUT,
    trader_id: str = DEFAULT_TRADER_ID_FUBON_XINDIAN,
    amount_threshold_ntd: float = DEFAULT_READINESS_AMOUNT_NTD,
) -> dict[str, Any]:
    """Readiness check for frozen amount · Leading · L1H8 · one-slot.

    Historical gates use data strictly before FORWARD_START. Untouched forward
    starts at FORWARD_START and is evaluated objectively (never hard-coded True).
    Default candidate is 50M (40M remains reproducible via amount_threshold_ntd).
    """
    if amount_threshold_ntd <= 0:
        raise ValueError("amount_threshold_ntd must be > 0")
    historical_end = (date.fromisoformat(FORWARD_START) - timedelta(days=1)).isoformat()
    hist_window_end = min(window_end, historical_end)
    forward_end = window_end if window_end >= FORWARD_START else None
    span_end = max(hist_window_end, window_end)

    candidate = FilterConfig(
        metric="amount",
        threshold=float(amount_threshold_ntd),
        min_avg_volume_shares=200_000.0,
        w20_momentum_min=None,
        rs_ratio_min=None,
        rrg_quadrant="leading",
        top_n=1,
        entry_lag=0,
        hold=8,
        n_slots=1,
    )
    mother_min = min(30_000_000.0, float(amount_threshold_ntd))
    raw = build_raw_xindian_amount_grouped(
        conn,
        window_start=window_start,
        window_end=span_end,
        min_net_amount_ntd=mother_min,
        trader_id=trader_id,
    )
    features = build_funnel_feature_index(conn, raw)
    hist_end_exclusive = FORWARD_START

    costs: dict[str, dict[str, Any]] = {}
    for cost_bps in (0.0, 60.0, 100.0):
        arm = _run_readiness_config(
            conn,
            raw,
            features,
            candidate,
            window_start=window_start,
            window_end=hist_window_end,
            oos_cut=oos_cut,
            cost_bps=cost_bps,
        )
        arm.pop("_day_dicts", None)
        costs[f"{cost_bps:g}bps"] = arm

    neighborhood: list[dict[str, Any]] = []
    for threshold in (30_000_000.0, 40_000_000.0, 50_000_000.0):
        for hold in (7, 8, 9):
            arm = _run_readiness_config(
                conn,
                raw,
                features,
                replace(candidate, threshold=threshold, hold=hold),
                window_start=window_start,
                window_end=hist_window_end,
                oos_cut=oos_cut,
                cost_bps=60.0,
            )
            arm.pop("_day_dicts", None)
            neighborhood.append(arm)

    candidate_cost60 = _run_readiness_config(
        conn,
        raw,
        features,
        candidate,
        window_start=window_start,
        window_end=hist_window_end,
        oos_cut=oos_cut,
        cost_bps=60.0,
    )
    fold_bounds = (
        ("2024H2", "2024-07-01", "2025-01-01"),
        ("2025H1", "2025-01-01", "2025-07-01"),
        ("2025H2", "2025-07-01", "2026-01-01"),
        ("2026H1+", "2026-01-01", hist_end_exclusive),
    )
    folds = []
    for label, start, end in fold_bounds:
        if start > hist_window_end or end <= window_start:
            continue
        fold = _readiness_slice(
            conn,
            candidate_cost60["_day_dicts"],
            start=max(start, window_start),
            end=min(end, hist_end_exclusive),
        )
        fold["label"] = label
        folds.append(fold)
    candidate_cost60.pop("_day_dicts", None)

    forward_metrics: dict[str, Any] | None = None
    forward_trading_days = 0
    if forward_end is not None:
        forward_trading_days = _count_trading_days(
            conn, start=FORWARD_START, end_inclusive=forward_end
        )
        forward_arm = _run_readiness_config(
            conn,
            raw,
            features,
            candidate,
            window_start=window_start,
            window_end=forward_end,
            oos_cut=oos_cut,
            cost_bps=FORWARD_COST_BPS,
        )
        forward_end_exclusive = (
            date.fromisoformat(forward_end) + timedelta(days=1)
        ).isoformat()
        forward_metrics = _readiness_slice(
            conn,
            forward_arm["_day_dicts"],
            start=FORWARD_START,
            end=forward_end_exclusive,
        )

    forward_gate = evaluate_forward_gate(
        trading_days=forward_trading_days,
        forward_metrics=forward_metrics,
    )
    readiness = evaluate_strategy_readiness_gates(
        cost_60=candidate_cost60,
        neighborhood=neighborhood,
        folds=folds,
        untouched_forward_available=bool(forward_gate["passed"]),
    )
    amount_m = float(amount_threshold_ntd) / 1e6
    return {
        "schema": "fubon_xindian_strategy_readiness-v1",
        "contract": {
            "window_start": window_start,
            "window_end": window_end,
            "historical_window_end": hist_window_end,
            "forward_start": FORWARD_START,
            "oos_cut": oos_cut,
            "trader_id": trader_id,
            "amount_threshold_ntd": float(amount_threshold_ntd),
            "amount_m": amount_m,
            "candidate": asdict(candidate),
            "selection_metric": "single-trade median/trimmed mean; not compound",
            "cost_bps": [0.0, 60.0, 100.0],
            "parameter_neighborhood": {
                "amount_m": [30, 40, 50],
                "hold_days": [7, 8, 9],
            },
            "selection_leakage": (
                "Amount/gate/H was chosen after reviewing all history through "
                f"{historical_end}; historical OOS is robustness only. "
                f"Untouched forward begins at {FORWARD_START}."
            ),
        },
        "cost_sensitivity": costs,
        "parameter_neighborhood": neighborhood,
        "time_folds_cost60": folds,
        "readiness": readiness,
        "forward_gate": forward_gate,
        "strategy_action": (
            "No Strategy/Order change until an untouched forward window passes."
            if not readiness["adoption_passed"]
            else (
                f"Historical + forward gates passed for {amount_m:g}M Leading "
                "L1H8; Strategy adoption may be considered separately."
            )
        ),
    }


def render_markdown(payload: dict[str, Any]) -> str:
    c = payload["contract"]
    schema = str(payload.get("schema") or "")
    if schema == "fubon_xindian_strategy_readiness-v1":
        readiness = payload["readiness"]
        candidate = c["candidate"]
        cost60 = payload["cost_sensitivity"]["60bps"]["metrics"]
        return "\n".join(
            [
                "# 富邦新店 · Strategy readiness",
                "",
                f"- window: `{c['window_start']}` → `{c['window_end']}` · "
                f"historical end `{c.get('historical_window_end')}` · "
                f"forward `{c.get('forward_start')}` · "
                f"historical OOS `{c['oos_cut']}`",
                f"- amount: `{c.get('amount_m')}M`",
                f"- candidate: `{candidate}`",
                "- objective: single-trade median / trimmed mean; compound is not a gate",
                "",
                "## Decision",
                "",
                f"- `{readiness['decision']}`",
                f"- historical gates passed: `{readiness['historical_passed']}`",
                f"- adoption passed: `{readiness['adoption_passed']}`",
                f"- checks: `{readiness['checks']}`",
                "",
                "## 60 bps stress",
                "",
                f"- full: `{cost60['full']}`",
                f"- historical OOS: `{cost60['oos']}`",
                "",
                "## Forward gate",
                "",
                f"- passed: `{payload['forward_gate'].get('passed')}`",
                f"- trading_days: `{payload['forward_gate'].get('trading_days')}`",
                f"- checks: `{payload['forward_gate'].get('checks')}`",
                f"- metrics: `{payload['forward_gate'].get('metrics')}`",
                "",
                (
                    "Strategy adoption eligible."
                    if readiness.get("adoption_passed")
                    else "Research observe only · no Strategy / Order change."
                ),
                "",
            ]
        )
    if "1slot" in schema:
        title = "# 富邦新店 · 金額版單槽優化 · 固定總本金"
    elif schema.startswith("fubon_xindian_amount_"):
        title = "# 富邦新店 · 金額版逐層優化 × 3 槽"
    else:
        title = "# 富邦新店 · 原始過濾器 × W20 × 3 槽"
    lines = [
        title,
        "",
        f"- window: `{c['window_start']}` → `{c['window_end']}` · OOS `{c['oos_cut']}`",
        f"- filter order: `{c['filter_order']}`",
        "- parameters selected on IS only; OOS replay",
        "",
        "## Hypotheses",
        "",
    ]
    for hid, hypothesis in payload["hypotheses"].items():
        lines.append(f"- **{hid}**: `{hypothesis['status']}`")
    lines.extend(["", "## Sequential stages", ""])
    for stage in payload["stages"]:
        selected = stage.get("selected_on_is") or {}
        config = selected.get("config") or {}
        is_metrics = (selected.get("metrics") or {}).get("is") or {}
        oos_metrics = (selected.get("metrics") or {}).get("oos") or {}
        lines.append(
            f"- `{stage['stage']}` → `{selected.get('config_id')}` · "
            f"OOS `{stage['oos_gate']['status']}` · "
            f"IS cycles={is_metrics.get('n_cycles')} α/筆={is_metrics.get('alpha_per_cycle')} · "
            f"OOS cycles={oos_metrics.get('n_cycles')} α/筆={oos_metrics.get('alpha_per_cycle')} · "
            f"config={config}"
        )
    final = payload["final_is_selected"]
    lines.extend(
        [
            "",
            "## Final IS-selected pipeline",
            "",
            f"- `{final['config_id']}`",
            f"- full: {final['metrics']['full']}",
            f"- OOS: {final['metrics']['oos']}",
            "",
            "Research only · not adopted into Strategy / Order layer.",
            "",
        ]
    )
    return "\n".join(lines)


def write_artifacts(
    payload: dict[str, Any],
    *,
    out_dir: Path | None = None,
    stamp: str | None = None,
    file_stem: str = FILE_STEM,
) -> dict[str, Path]:
    out = out_dir or (REPORTS_RESEARCH / "fubon-xindian-copytrade")
    out.mkdir(parents=True, exist_ok=True)
    prefix = stamp or date.today().strftime("%Y%m%d")
    stem = out / f"{prefix}_{file_stem}"
    json_path = Path(str(stem) + ".json")
    md_path = Path(str(stem) + ".md")
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return {"json": json_path, "md": md_path}


def _yahoo_url(stock_id: str) -> str:
    return f"https://tw.stock.yahoo.com/quote/{stock_id}.TW/technical-analysis"


def build_amount_candidate_trades_payload(
    conn: sqlite3.Connection,
    *,
    config: FilterConfig,
    window_start: str,
    window_end: str,
    trader_id: str = DEFAULT_TRADER_ID_FUBON_XINDIAN,
    capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
) -> dict[str, Any]:
    """Replay gate-surviving amount candidate → champion-style trades JSON."""
    raw = build_raw_xindian_grouped(
        conn,
        window_start=window_start,
        window_end=window_end,
        trader_id=trader_id,
    )
    features = build_funnel_feature_index(conn, raw)
    grouped, funnel = build_config_grouped(raw, features, config)
    result = run_copytrade_backtest(
        conn,
        ETF_CODE_PROXY,
        strategy_id=f"L{config.entry_lag + 1}H{config.hold}",
        strategy_label=config.id,
        entry_lag_days=config.entry_lag,
        hold_trading_days=config.hold,
        entry_price_mode="open",
        capital_ntd=capital_ntd,
        cost_bps=0.0,
        window_start=window_start,
        window_end=window_end,
        grouped=grouped,
    )
    day_by_sig = {d.signal_date: d for d in result.signal_days}
    day_dicts = day_results_to_dicts(result)
    executed, meta = select_executed_signal_days(day_dicts, n_slots=config.n_slots)

    name_map = {
        str(r[0]): str(r[1])
        for r in conn.execute(
            """
            SELECT stock_id, stock_name
            FROM etf_holdings
            WHERE stock_name IS NOT NULL AND TRIM(stock_name) != ''
            GROUP BY stock_id
            """
        ).fetchall()
    }

    trades: list[dict[str, Any]] = []
    for i, day in enumerate(executed, 1):
        sig_d = str(day["signal_date"])
        full = day_by_sig.get(sig_d)
        if full is None:
            continue
        for lg in full.legs:
            if lg.status != "complete":
                continue
            sid = str(lg.stock_id)
            feat = features.get((sig_d, sid)) or {}
            trades.append(
                {
                    "cycle": i,
                    "signal_date": lg.signal_date,
                    "stock_id": sid,
                    "stock_name": name_map.get(sid) or lg.stock_name or sid,
                    "net_lots": round(float(lg.share_delta) / SHARES_PER_LOT, 2),
                    "entry_date": lg.entry_date,
                    "entry_px": lg.entry_px,
                    "exit_date": lg.exit_date,
                    "exit_px": lg.exit_px,
                    "return_pct": round(float(lg.return_pct), 4),
                    "pnl_ntd": round(float(lg.pnl_ntd), 2),
                    "allocated_ntd": round(float(lg.allocated_ntd), 2),
                    "bench_return_pct": round(float(full.bench_return_pct), 4),
                    "basket_alpha_ntd": round(float(full.alpha_ntd), 2),
                    "rrg_quadrant": feat.get("quadrant"),
                    "rs_ratio": feat.get("rs_ratio"),
                    "rs_momentum": feat.get("rs_momentum"),
                    "yahoo_url": _yahoo_url(sid),
                }
            )

    by_month: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        month = str(t.get("exit_date") or "")[:7]
        if len(month) == 7:
            by_month.setdefault(month, []).append(t)
    monthly: list[dict[str, Any]] = []
    for month in sorted(by_month):
        rows = by_month[month]
        wins = sum(1 for r in rows if float(r["return_pct"]) > 0)
        monthly.append(
            {
                "month": month,
                "n_trades": len(rows),
                "pnl_ntd": round(sum(float(r["pnl_ntd"]) for r in rows), 2),
                "alpha_ntd": round(
                    sum(float(r["basket_alpha_ntd"]) for r in rows), 2
                ),
                "win_rate_pct": round(100.0 * wins / len(rows), 1) if rows else None,
                "avg_return_pct": round(
                    sum(float(r["return_pct"]) for r in rows) / len(rows), 2
                )
                if rows
                else None,
            }
        )

    neg_streak = 0
    max_neg = 0
    for m in monthly:
        if float(m["pnl_ntd"]) < 0:
            neg_streak += 1
            max_neg = max(max_neg, neg_streak)
        else:
            neg_streak = 0
    positive_months = sum(1 for m in monthly if float(m["pnl_ntd"]) > 0)
    wins = sum(1 for t in trades if float(t["return_pct"]) > 0)
    sum_pnl = round(sum(float(t["pnl_ntd"]) for t in trades), 2)
    total_capital = capital_ntd * config.n_slots
    threshold_label = (
        f"{config.threshold / 1e6:g}M"
        if config.metric == "amount"
        else f"{config.threshold / SHARES_PER_LOT:g}lots"
    )
    return {
        "champion": {
            "mode": "xd",
            "metric": config.metric,
            "threshold": threshold_label,
            "rrg_gate": (
                " · ".join(
                    part
                    for part in (
                        (
                            f"rs_ratio>={config.rs_ratio_min:g}"
                            if config.rs_ratio_min is not None
                            else None
                        ),
                        (
                            f"w20_momentum>={config.w20_momentum_min:g}"
                            if config.w20_momentum_min is not None
                            else None
                        ),
                    )
                    if part
                )
                or "none"
            ),
            "strategy_id": f"L{config.entry_lag + 1}H{config.hold}",
            "n_slots": config.n_slots,
            "top_n": config.top_n,
            "window_start": window_start,
            "window_end": window_end,
            "horizon_note": (
                f"L{config.entry_lag + 1} 進場、持有 {config.hold} 交易日"
                f"≈{config.hold / 5:.1f} 週，屬短線偏短中線（非月線波段）"
            ),
            "period_return_pct": (
                round(100.0 * sum_pnl / total_capital, 4) if total_capital > 0 else None
            ),
            "capital_ntd_per_slot": capital_ntd,
            "config_id": config.id,
        },
        "meta": {
            "n_signals": meta.get("n_signals"),
            "recycled_n_cycles": meta.get("recycled_n_cycles"),
            "n_skipped": meta.get("n_skipped"),
            "signal_capture_pct": meta.get("signal_capture_pct"),
            "n_trade_legs": len(trades),
            "leg_win_rate_pct": (
                round(100.0 * wins / len(trades), 2) if trades else None
            ),
            "sum_pnl_ntd": sum_pnl,
            "positive_months": positive_months,
            "n_months": len(monthly),
            "positive_month_pct": (
                round(100.0 * positive_months / len(monthly), 1) if monthly else None
            ),
            "max_negative_month_streak": max_neg,
            "funnel": funnel,
            "trader_id": trader_id,
        },
        "monthly": monthly,
        "trades": trades,
    }
