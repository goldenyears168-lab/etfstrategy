"""金額 × RRG Copytrade：3 槽與 Leading Dip 正交漏斗研究。

研究契約：
* 基準：富邦新店金額門檻、Top1、RS-ratio>100、L2H12。
* 參數只在 IS 選擇；OOS 僅作一次否證。
* Leading Dip 概念移植到訊號日收盤可知的日頻特徵，不使用未來的盤中觸發。
"""

from __future__ import annotations

import json
import math
import sqlite3
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from copytrade.signals import CopytradeSignal
from market_benchmark import load_benchmark_close
from report_paths import REPORTS_RESEARCH
from research.backtest.amount_rrg_copytrade_study import (
    ETF_CODE_PROXY,
    apply_rrg_gate,
    build_rrg_ffill_index,
    build_signals_fixed,
    load_rrg_close_index,
)
from research.backtest.copytrade_backtest import (
    DEFAULT_SIGNAL_CAPITAL_NTD,
    count_hold_trading_days,
    run_copytrade_backtest,
    select_executed_signal_days,
)
from research.backtest.dual_branch_copytrade_sweep import day_results_to_dicts
from research.backtest.finpilot_local_backtest import load_price_panels
from rrg_rotation import compute_rrg_panel

DEFAULT_WINDOW_START = "2024-07-01"
DEFAULT_OOS_CUT = "2025-10-01"
DEFAULT_WINDOW_END = "2026-07-17"
DEFAULT_AMOUNT_NTD = 80_000_000.0
DEFAULT_SLOTS = 3
DEFAULT_ENTRY_LAG = 1
DEFAULT_HOLD = 12
FILE_STEM = "amount_rrg_3slot_funnel_study"


@dataclass(frozen=True)
class FunnelSpec:
    id: str
    family: str
    label: str
    feature: str
    op: str
    threshold: float | str


def _passes(value: Any, spec: FunnelSpec) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    if spec.op == "le":
        return float(value) <= float(spec.threshold)
    if spec.op == "lt":
        return float(value) < float(spec.threshold)
    if spec.op == "ge":
        return float(value) >= float(spec.threshold)
    if spec.op == "eq":
        return str(value) == str(spec.threshold)
    if spec.op == "ne":
        return str(value) != str(spec.threshold)
    if spec.op == "true":
        return bool(value)
    raise ValueError(f"unknown funnel op: {spec.op}")


def phi_binary(a: list[bool], b: list[bool]) -> float | None:
    """Phi correlation for two binary masks."""
    if len(a) != len(b) or len(a) < 3:
        return None
    n11 = sum(x and y for x, y in zip(a, b))
    n10 = sum(x and not y for x, y in zip(a, b))
    n01 = sum(not x and y for x, y in zip(a, b))
    n00 = len(a) - n11 - n10 - n01
    den = math.sqrt((n11 + n10) * (n01 + n00) * (n11 + n01) * (n10 + n00))
    if den == 0:
        return None
    return (n11 * n00 - n10 * n01) / den


def max_drawdown_ntd(pnls: list[float]) -> float:
    """Maximum peak-to-trough drawdown on additive fixed-notional PnL."""
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for pnl in pnls:
        equity += float(pnl)
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(worst, 2)


def _asof_frame_value(
    frame: pd.DataFrame,
    *,
    stock_id: str,
    signal_date: str,
) -> float | None:
    if stock_id not in frame.columns:
        return None
    series = frame[stock_id].loc[:signal_date].dropna()
    if series.empty:
        return None
    return float(series.iloc[-1])


def _exact_frame_value(
    frame: pd.DataFrame,
    *,
    stock_id: str,
    signal_date: str,
) -> float | None:
    if stock_id not in frame.columns or signal_date not in frame.index:
        return None
    value = frame.at[signal_date, stock_id]
    return float(value) if pd.notna(value) else None


def build_funnel_feature_index(
    conn: sqlite3.Connection,
    grouped: dict[str, list[CopytradeSignal]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Build signal-day-close PIT features for all candidate stock-days."""
    close, _, volume = load_price_panels(conn)
    close.index = close.index.astype(str)
    volume.index = volume.index.astype(str)
    benchmark = load_benchmark_close(conn).reindex(close.index).astype(float)
    rv5, mv5, _ = compute_rrg_panel(close, benchmark, length=5)
    avg_volume20 = volume.rolling(20, min_periods=10).mean()

    stock_ret1 = close.pct_change(1, fill_method=None) * 100.0
    stock_ret3 = close.pct_change(3, fill_method=None) * 100.0
    bench_ret1 = benchmark.pct_change(1, fill_method=None) * 100.0
    bench_ret3 = benchmark.pct_change(3, fill_method=None) * 100.0
    bench_ret20 = benchmark.pct_change(20, fill_method=None) * 100.0

    rows = conn.execute(
        """
        SELECT session_date, stock_id, rs_ratio, rs_momentum, quadrant, trend
        FROM rrg_universe_scores
        WHERE screen_kind='close'
        ORDER BY stock_id, session_date
        """
    ).fetchall()
    rrg_by_stock: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for row in rows:
        sid = str(row[1])
        rrg_by_stock[sid].append(
            (
                str(row[0]),
                {
                    "rs_ratio": float(row[2]) if row[2] is not None else None,
                    "rs_momentum": float(row[3]) if row[3] is not None else None,
                    "quadrant": str(row[4]) if row[4] is not None else None,
                    "trend20": str(row[5]) if row[5] is not None else None,
                },
            )
        )
    rrg_dates = {
        sid: [day for day, _ in series] for sid, series in rrg_by_stock.items()
    }

    def rrg_asof(sid: str, day: str) -> dict[str, Any]:
        series = rrg_by_stock.get(sid, [])
        pos = bisect_right(rrg_dates.get(sid, []), day) - 1
        return series[pos][1] if pos >= 0 else {}

    out: dict[tuple[str, str], dict[str, Any]] = {}
    for day, legs in grouped.items():
        for leg in legs:
            sid = str(leg.stock_id)
            daily1 = _asof_frame_value(stock_ret1, stock_id=sid, signal_date=day)
            daily3 = _asof_frame_value(stock_ret3, stock_id=sid, signal_date=day)
            b1 = float(bench_ret1.loc[day]) if day in bench_ret1.index and pd.notna(bench_ret1.loc[day]) else None
            b3 = float(bench_ret3.loc[day]) if day in bench_ret3.index and pd.notna(bench_ret3.loc[day]) else None
            b20 = float(bench_ret20.loc[day]) if day in bench_ret20.index and pd.notna(bench_ret20.loc[day]) else None
            snap = rrg_asof(sid, day)
            rv = snap.get("rs_ratio")
            mv = snap.get("rs_momentum")
            out[(day, sid)] = {
                **snap,
                "signal_close": _exact_frame_value(
                    close, stock_id=sid, signal_date=day
                ),
                "avg_volume20": _asof_frame_value(
                    avg_volume20, stock_id=sid, signal_date=day
                ),
                "w5_momentum": _asof_frame_value(mv5, stock_id=sid, signal_date=day),
                "w5_ratio": _asof_frame_value(rv5, stock_id=sid, signal_date=day),
                "excess_1d": (
                    float(daily1) - float(b1)
                    if daily1 is not None and b1 is not None
                    else None
                ),
                "excess_3d": (
                    float(daily3) - float(b3)
                    if daily3 is not None and b3 is not None
                    else None
                ),
                "not_up_right": snap.get("trend20") != "up_right"
                if snap.get("trend20") is not None
                else None,
                "geo_below_yx": (
                    rv is not None
                    and mv is not None
                    and float(rv) > 100.0
                    and float(mv) > 100.0
                    and float(mv) < float(rv)
                ),
                "mild_down_market": (
                    b20 is not None and -5.0 <= float(b20) < 0.0
                ),
                "bench_ret20": b20,
            }
    return out


def apply_funnel_specs(
    grouped: dict[str, list[CopytradeSignal]],
    features: dict[tuple[str, str], dict[str, Any]],
    specs: tuple[FunnelSpec, ...],
) -> tuple[dict[str, list[CopytradeSignal]], dict[str, int]]:
    out: dict[str, list[CopytradeSignal]] = {}
    missing = 0
    dropped = 0
    for day, legs in grouped.items():
        kept: list[CopytradeSignal] = []
        for leg in legs:
            feat = features.get((day, str(leg.stock_id)))
            if feat is None:
                missing += 1
                continue
            ok = True
            for spec in specs:
                if spec.feature not in feat or feat[spec.feature] is None:
                    missing += 1
                    ok = False
                    break
                if not _passes(feat[spec.feature], spec):
                    ok = False
                    break
            if ok:
                kept.append(leg)
            else:
                dropped += 1
        if kept:
            out[day] = kept
    return out, {"missing": missing, "dropped": dropped, "kept_days": len(out)}


def _metrics(
    conn: sqlite3.Connection,
    day_dicts: list[dict[str, Any]],
    *,
    n_slots: int,
    capital_ntd: float,
    start: str,
    end: str,
) -> dict[str, Any]:
    complete = [
        d
        for d in day_dicts
        if d.get("status") == "complete"
        and start <= str(d.get("signal_date") or "") < end
    ]
    executed, slot_meta = select_executed_signal_days(complete, n_slots=n_slots)
    pnl = sum(float(d.get("pnl_ntd") or 0.0) for d in executed)
    alpha = sum(float(d.get("alpha_ntd") or 0.0) for d in executed)
    locked = sum(
        count_hold_trading_days(conn, str(d["entry_date"]), str(d["exit_date"]))
        for d in executed
    )
    months: dict[str, float] = defaultdict(float)
    for d in executed:
        months[str(d.get("exit_date") or "")[:7]] += float(d.get("pnl_ntd") or 0.0)
    n_months = len(months)
    positive_months = sum(v > 0 for v in months.values())
    trading_days = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT trade_date)
            FROM stock_daily_bars
            WHERE trade_date>=? AND trade_date<?
            """,
            (start, end),
        ).fetchone()[0]
        or 0
    )
    cycles = len(executed)
    return {
        "start": start,
        "end_exclusive": end,
        "n_signals": len(complete),
        "n_cycles": cycles,
        "n_skipped": int(slot_meta.get("n_skipped") or 0),
        "capture_pct": slot_meta.get("signal_capture_pct"),
        "peak_slots": slot_meta.get("peak_concurrent_slots"),
        "capital_utilization_pct": (
            round(100.0 * locked / (trading_days * n_slots), 2)
            if trading_days and n_slots
            else None
        ),
        "pnl_ntd": round(pnl, 2),
        "alpha_ntd": round(alpha, 2),
        "period_return_pct": round(100.0 * pnl / (capital_ntd * n_slots), 4),
        "alpha_per_cycle": round(alpha / cycles, 2) if cycles else None,
        "alpha_per_locked_day": round(alpha / locked, 4) if locked else None,
        "win_rate_pct": (
            round(100.0 * sum(float(d.get("pnl_ntd") or 0) > 0 for d in executed) / cycles, 2)
            if cycles
            else None
        ),
        "positive_month_pct": (
            round(100.0 * positive_months / n_months, 2) if n_months else None
        ),
        "n_months": n_months,
        "max_drawdown_ntd": max_drawdown_ntd(
            [float(d.get("pnl_ntd") or 0.0) for d in executed]
        ),
    }


def _run_arm(
    conn: sqlite3.Connection,
    grouped: dict[str, list[CopytradeSignal]],
    *,
    label: str,
    n_slots: int,
    capital_ntd: float,
    window_start: str,
    window_end: str,
    oos_cut: str,
    entry_lag: int = DEFAULT_ENTRY_LAG,
    hold: int = DEFAULT_HOLD,
    etf_code_proxy: str = ETF_CODE_PROXY,
) -> dict[str, Any]:
    result = run_copytrade_backtest(
        conn,
        etf_code_proxy,
        strategy_id=f"L{entry_lag + 1}H{hold}",
        strategy_label=label,
        entry_lag_days=entry_lag,
        hold_trading_days=hold,
        entry_price_mode="open",
        capital_ntd=capital_ntd,
        cost_bps=0.0,
        window_start=window_start,
        window_end=window_end,
        grouped=grouped,
    )
    day_dicts = day_results_to_dicts(result)
    # Any date after window_end works as an exclusive upper bound.
    end_exclusive = f"{int(window_end[:4]) + 1:04d}-01-01"
    return {
        "full": _metrics(
            conn,
            day_dicts,
            n_slots=n_slots,
            capital_ntd=capital_ntd,
            start=window_start,
            end=end_exclusive,
        ),
        "is": _metrics(
            conn,
            day_dicts,
            n_slots=n_slots,
            capital_ntd=capital_ntd,
            start=window_start,
            end=oos_cut,
        ),
        "oos": _metrics(
            conn,
            day_dicts,
            n_slots=n_slots,
            capital_ntd=capital_ntd,
            start=oos_cut,
            end=end_exclusive,
        ),
    }


def _grid_specs() -> dict[str, list[FunnelSpec]]:
    return {
        "pullback_1d": [
            FunnelSpec(
                id=f"ex1_le_{v:g}",
                family="pullback_1d",
                label=f"訊號日個股−大盤 1 日報酬 ≤ {v:g}pp",
                feature="excess_1d",
                op="le",
                threshold=float(v),
            )
            for v in (1.0, 0.0, -1.0, -2.0, -3.0)
        ],
        "pullback_3d": [
            FunnelSpec(
                id=f"ex3_le_{v:g}",
                family="pullback_3d",
                label=f"訊號日個股−大盤 3 日報酬 ≤ {v:g}pp",
                feature="excess_3d",
                op="le",
                threshold=float(v),
            )
            for v in (2.0, 0.0, -2.0, -4.0, -6.0)
        ],
        "avoid_deep_1d": [
            FunnelSpec(
                id=f"ex1_ge_{v:g}",
                family="avoid_deep_1d",
                label=f"訊號日個股−大盤 1 日報酬 ≥ {v:g}pp",
                feature="excess_1d",
                op="ge",
                threshold=float(v),
            )
            for v in (-5.0, -3.0, -2.0, -1.0, 0.0)
        ],
        "avoid_deep_3d": [
            FunnelSpec(
                id=f"ex3_ge_{v:g}",
                family="avoid_deep_3d",
                label=f"訊號日個股−大盤 3 日報酬 ≥ {v:g}pp",
                feature="excess_3d",
                op="ge",
                threshold=float(v),
            )
            for v in (-8.0, -6.0, -4.0, -2.0, 0.0)
        ],
        "w5_weak": [
            FunnelSpec(
                id=f"w5mv_lt_{v:g}",
                family="w5_weak",
                label=f"W5 RS-Momentum < {v:g}",
                feature="w5_momentum",
                op="lt",
                threshold=float(v),
            )
            for v in (98.0, 100.0, 102.0, 105.0)
        ],
        "w5_strong": [
            FunnelSpec(
                id=f"w5mv_ge_{v:g}",
                family="w5_strong",
                label=f"W5 RS-Momentum ≥ {v:g}",
                feature="w5_momentum",
                op="ge",
                threshold=float(v),
            )
            for v in (95.0, 98.0, 100.0, 102.0)
        ],
        "w20_momentum": [
            FunnelSpec(
                id=f"w20mv_ge_{v:g}",
                family="w20_momentum",
                label=f"W20 RS-Momentum ≥ {v:g}",
                feature="rs_momentum",
                op="ge",
                threshold=float(v),
            )
            for v in (98.0, 99.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0)
        ],
        "not_up_right": [
            FunnelSpec(
                id="w20_not_up_right",
                family="not_up_right",
                label="W20 軌跡非 up_right",
                feature="not_up_right",
                op="true",
                threshold=1.0,
            )
        ],
        "up_right_control": [
            FunnelSpec(
                id="w20_up_right",
                family="up_right_control",
                label="W20 軌跡為 up_right（反向控制）",
                feature="trend20",
                op="eq",
                threshold="up_right",
            )
        ],
        "geo_below_yx": [
            FunnelSpec(
                id="geo_leading_below_yx",
                family="geo_below_yx",
                label="W20 Leading 且 Momentum<Ratio",
                feature="geo_below_yx",
                op="true",
                threshold=1.0,
            )
        ],
    }


def _select_is_arm(
    arms: list[dict[str, Any]],
    baseline: dict[str, Any],
) -> dict[str, Any] | None:
    base_cycles = int(baseline["is"].get("n_cycles") or 0)
    base_apc = baseline["is"].get("alpha_per_cycle")
    viable = [
        arm
        for arm in arms
        if int(arm["metrics"]["is"].get("n_cycles") or 0) >= max(8, int(base_cycles * 0.5))
        and arm["metrics"]["is"].get("alpha_per_cycle") is not None
        and (
            base_apc is None
            or float(arm["metrics"]["is"]["alpha_per_cycle"])
            >= float(base_apc) * 1.05
        )
    ]
    if not viable:
        return None
    return max(
        viable,
        key=lambda arm: (
            float(arm["metrics"]["is"].get("alpha_per_cycle") or -1e18),
            float(arm["metrics"]["is"].get("alpha_per_locked_day") or -1e18),
            float(arm["metrics"]["is"].get("alpha_ntd") or -1e18),
        ),
    )


def _oos_gate(
    selected: dict[str, Any] | None,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    if selected is None:
        return {"status": "insufficient_n", "passed": False, "reasons": ["IS 無可選參數"]}
    arm = selected["metrics"]["oos"]
    base = baseline["oos"]
    arm_cycles = int(arm.get("n_cycles") or 0)
    base_cycles = int(base.get("n_cycles") or 0)
    arm_apc = arm.get("alpha_per_cycle")
    base_apc = base.get("alpha_per_cycle")
    arm_alpha = float(arm.get("alpha_ntd") or 0.0)
    base_alpha = float(base.get("alpha_ntd") or 0.0)
    arm_month = arm.get("positive_month_pct")
    base_month = base.get("positive_month_pct")
    checks = {
        "oos_n_ge_8": arm_cycles >= 8,
        "retain_cycles_ge_50pct": arm_cycles >= max(1, int(base_cycles * 0.5)),
        "alpha_per_cycle_lift_ge_10pct": (
            arm_apc is not None
            and base_apc is not None
            and float(arm_apc) >= float(base_apc) * 1.10
        ),
        "retain_total_alpha_ge_70pct": (
            base_alpha <= 0 or arm_alpha >= base_alpha * 0.70
        ),
        "positive_month_not_worse_5pt": (
            arm_month is not None
            and base_month is not None
            and float(arm_month) >= float(base_month) - 5.0
        ),
    }
    passed = all(checks.values())
    return {
        "status": "passed" if passed else "rejected",
        "passed": passed,
        "checks": checks,
        "delta_oos_alpha_per_cycle": (
            None
            if arm_apc is None or base_apc is None
            else round(float(arm_apc) - float(base_apc), 2)
        ),
        "delta_oos_pnl_ntd": round(
            float(arm.get("pnl_ntd") or 0) - float(base.get("pnl_ntd") or 0), 2
        ),
    }


def run_three_slot_funnel_study(
    conn: sqlite3.Connection,
    *,
    window_start: str = DEFAULT_WINDOW_START,
    window_end: str = DEFAULT_WINDOW_END,
    oos_cut: str = DEFAULT_OOS_CUT,
    capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
    force_amount_ntd: float | None = None,
) -> dict[str, Any]:
    """Run amount baseline tuning, 1→3 slots, and one-layer-at-a-time funnels."""
    tape_end = window_end
    from copytrade.dual_branch_signals import DualBranchTapeCache

    tape = DualBranchTapeCache.load(
        conn, window_start=window_start, window_end=tape_end, need_closes=True
    )
    calendar = sorted(set(tape.sj) | set(tape.xd))
    raw_rrg = load_rrg_close_index(
        conn, window_start=window_start, window_end=window_end
    )
    rrg_ffill = build_rrg_ffill_index(raw_rrg, calendar)

    thresholds = (50_000_000.0, 65_000_000.0, 80_000_000.0, 100_000_000.0)
    amount_arms: list[dict[str, Any]] = []
    grouped_by_amount: dict[float, dict[str, list[CopytradeSignal]]] = {}
    for amount in thresholds:
        raw = build_signals_fixed(
            conn,
            mode="xd",
            min_threshold=amount,
            top_n=1,
            window_start=window_start,
            window_end=window_end,
            tape=tape,
            min_avg_volume_shares=200_000.0,
        )
        grouped, _, drops = apply_rrg_gate(raw, rrg_ffill, "rs_ratio_gt100")
        grouped_by_amount[amount] = grouped
        metrics = _run_arm(
            conn,
            grouped,
            label=f"xd amount≥{amount/1e6:.0f}M rs_ratio>100 L2H12 slots3",
            n_slots=DEFAULT_SLOTS,
            capital_ntd=capital_ntd,
            window_start=window_start,
            window_end=window_end,
            oos_cut=oos_cut,
        )
        amount_arms.append(
            {
                "amount_ntd": amount,
                "rrg_drops": drops,
                "metrics": metrics,
            }
        )

    fixed80 = next(a for a in amount_arms if a["amount_ntd"] == DEFAULT_AMOUNT_NTD)
    base_is_apc = float(fixed80["metrics"]["is"].get("alpha_per_cycle") or 0.0)
    viable_amount = [
        a
        for a in amount_arms
        if int(a["metrics"]["is"].get("n_cycles") or 0) >= 12
        and float(a["metrics"]["is"].get("alpha_per_cycle") or -1e18)
        >= base_is_apc * 0.90
    ]
    if force_amount_ntd is not None:
        selected_amount = next(
            (
                arm
                for arm in amount_arms
                if float(arm["amount_ntd"]) == float(force_amount_ntd)
            ),
            None,
        )
        if selected_amount is None:
            raise ValueError(
                f"force_amount_ntd must be one of {thresholds}, got {force_amount_ntd}"
            )
    else:
        selected_amount = max(
            viable_amount or [fixed80],
            key=lambda a: (
                float(a["metrics"]["is"].get("alpha_ntd") or -1e18),
                int(a["metrics"]["is"].get("n_cycles") or 0),
            ),
        )
    amount_ntd = float(selected_amount["amount_ntd"])
    baseline_grouped = grouped_by_amount[amount_ntd]
    baseline3 = selected_amount["metrics"]
    baseline1 = _run_arm(
        conn,
        baseline_grouped,
        label=f"xd amount≥{amount_ntd/1e6:.0f}M rs_ratio>100 L2H12 slots1",
        n_slots=1,
        capital_ntd=capital_ntd,
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
    )
    h_slots_checks = {
        "capture_improves": float(baseline3["oos"].get("capture_pct") or 0)
        > float(baseline1["oos"].get("capture_pct") or 0),
        "cycles_increase": int(baseline3["oos"].get("n_cycles") or 0)
        > int(baseline1["oos"].get("n_cycles") or 0),
        "alpha_per_cycle_preserved": float(
            baseline3["oos"].get("alpha_per_cycle") or -1e18
        )
        >= float(baseline1["oos"].get("alpha_per_cycle") or 0) * 0.90,
    }
    amount_stability_checks = {
        "oos_pnl_ge_90pct_of_80m": float(
            selected_amount["metrics"]["oos"].get("pnl_ntd") or 0
        )
        >= float(fixed80["metrics"]["oos"].get("pnl_ntd") or 0) * 0.90,
        "oos_alpha_per_cycle_ge_90pct_of_80m": float(
            selected_amount["metrics"]["oos"].get("alpha_per_cycle") or 0
        )
        >= float(fixed80["metrics"]["oos"].get("alpha_per_cycle") or 0) * 0.90,
    }

    features = build_funnel_feature_index(conn, baseline_grouped)
    family_results: list[dict[str, Any]] = []
    selected_specs: list[FunnelSpec] = []
    spec_grid = _grid_specs()
    for family, specs in spec_grid.items():
        arms: list[dict[str, Any]] = []
        for spec in specs:
            filtered, coverage = apply_funnel_specs(
                baseline_grouped, features, (spec,)
            )
            arms.append(
                {
                    "spec": {
                        "id": spec.id,
                        "family": spec.family,
                        "label": spec.label,
                        "feature": spec.feature,
                        "op": spec.op,
                        "threshold": spec.threshold,
                    },
                    "coverage": coverage,
                    "metrics": _run_arm(
                        conn,
                        filtered,
                        label=f"3slot/{spec.id}",
                        n_slots=DEFAULT_SLOTS,
                        capital_ntd=capital_ntd,
                        window_start=window_start,
                        window_end=window_end,
                        oos_cut=oos_cut,
                    ),
                }
            )
        chosen = _select_is_arm(arms, baseline3)
        gate = _oos_gate(chosen, baseline3)
        if chosen is None:
            base_cycles = int(baseline3["is"].get("n_cycles") or 0)
            min_cycles = max(8, int(base_cycles * 0.5))
            max_cycles = max(
                (int(a["metrics"]["is"].get("n_cycles") or 0) for a in arms),
                default=0,
            )
            gate = {
                "status": (
                    "insufficient_n" if max_cycles < min_cycles else "no_is_lift"
                ),
                "passed": False,
                "min_is_cycles": min_cycles,
                "max_is_cycles": max_cycles,
                "reason": (
                    "所有參數 IS α/筆皆未比基準提高 5%"
                    if max_cycles >= min_cycles
                    else "所有參數 IS 樣本保留不足"
                ),
            }
        if chosen is not None:
            selected_specs.append(next(s for s in specs if s.id == chosen["spec"]["id"]))
        family_results.append(
            {
                "family": family,
                "arms": arms,
                "selected_on_is": chosen,
                "oos_gate": gate,
            }
        )

    # Leading Dip structure bundle is intentionally separate: its two legs are
    # correlated and should be treated as one package rather than two "orthogonal" layers.
    notur = spec_grid["not_up_right"][0]
    w5 = next(
        s for s in spec_grid["w5_weak"] if float(s.threshold) == 100.0
    )
    bundle_result = None
    if w5 is not None:
        filtered, coverage = apply_funnel_specs(
            baseline_grouped, features, (notur, w5)
        )
        bundle_metrics = _run_arm(
            conn,
            filtered,
            label=f"3slot/{notur.id}+{w5.id}",
            n_slots=DEFAULT_SLOTS,
            capital_ntd=capital_ntd,
            window_start=window_start,
            window_end=window_end,
            oos_cut=oos_cut,
        )
        bundle_result = {
            "spec_ids": [notur.id, w5.id],
            "coverage": coverage,
            "metrics": bundle_metrics,
            "oos_gate": _oos_gate({"metrics": bundle_metrics}, baseline3),
        }

    candidate_keys = sorted(
        (day, str(leg.stock_id))
        for day, legs in baseline_grouped.items()
        for leg in legs
    )
    orthogonality: list[dict[str, Any]] = []
    for i, a in enumerate(selected_specs):
        for b in selected_specs[i + 1 :]:
            ma = [_passes(features.get(k, {}).get(a.feature), a) for k in candidate_keys]
            mb = [_passes(features.get(k, {}).get(b.feature), b) for k in candidate_keys]
            orthogonality.append(
                {
                    "a": a.id,
                    "b": b.id,
                    "phi": (
                        round(v, 3) if (v := phi_binary(ma, mb)) is not None else None
                    ),
                }
            )

    hypotheses = {
        "H0A_THREE_SLOTS_ADD_COVERAGE": {
            "status": (
                "passed"
                if h_slots_checks["capture_improves"]
                and h_slots_checks["cycles_increase"]
                else "rejected"
            ),
            "checks": h_slots_checks,
            "slots1": baseline1,
            "slots3": baseline3,
        },
        "H0B_THREE_SLOTS_PRESERVE_QUALITY": {
            "status": (
                "passed"
                if h_slots_checks["alpha_per_cycle_preserved"]
                else "rejected"
            ),
            "checks": h_slots_checks,
            "slots1": baseline1,
            "slots3": baseline3,
        },
        "H1_AMOUNT_THRESHOLD_FOR_3SLOT": {
            "status": (
                "passed" if all(amount_stability_checks.values()) else "rejected"
            ),
            "checks": amount_stability_checks,
            "selected_amount_ntd": amount_ntd,
            "fixed_80m": fixed80,
            "selected": selected_amount,
        },
    }
    for idx, family in enumerate(family_results, start=2):
        hypotheses[f"H{idx}_{family['family'].upper()}"] = {
            "status": family["oos_gate"]["status"],
            "selected_on_is": family["selected_on_is"],
            "oos_gate": family["oos_gate"],
        }
    bundle_hid = f"H{len(family_results) + 2}_LEADING_DIP_STRUCTURE_BUNDLE"
    hypotheses[bundle_hid] = (
        {
            "status": bundle_result["oos_gate"]["status"],
            **bundle_result,
        }
        if bundle_result is not None
        else {"status": "insufficient_n"}
    )

    return {
        "schema": "amount_rrg_3slot_funnel_study-v1",
        "contract": {
            "window_start": window_start,
            "window_end": window_end,
            "oos_cut": oos_cut,
            "selection": "parameters selected on IS only; OOS is falsification",
            "force_amount_ntd": force_amount_ntd,
            "baseline": "xd amount threshold · Top1 · RS-ratio>100 · L2H12",
            "slots": DEFAULT_SLOTS,
            "capital_ntd_per_slot": capital_ntd,
            "cost_bps": 0.0,
            "pit": "signal-day close features; entry T+2 open",
        },
        "amount_threshold_arms": amount_arms,
        "selected_amount_ntd": amount_ntd,
        "baseline_slots1": baseline1,
        "baseline_slots3": baseline3,
        "feature_coverage": {
            "candidate_legs": len(candidate_keys),
            "feature_rows": len(features),
        },
        "families": family_results,
        "structure_bundle": bundle_result,
        "orthogonality": orthogonality,
        "hypotheses": hypotheses,
        "market_stratum_note": (
            "20日大盤 mild-down 僅分層觀察，不作 hard gate；Regime layer 是診斷，不是 alpha。"
        ),
    }


def render_markdown(payload: dict[str, Any]) -> str:
    c = payload["contract"]
    lines = [
        "# 金額 × RRG Copytrade · 3 槽 × Leading Dip 正交漏斗",
        "",
        f"- Window: `{c['window_start']}` → `{c['window_end']}` · OOS cut `{c['oos_cut']}`",
        f"- Baseline: `{c['baseline']}` · 3 slots · cost 0 bps",
        f"- Selected amount (IS): **{payload['selected_amount_ntd']/1e6:.0f}M**",
        "",
        "## Hypotheses",
        "",
    ]
    for hid, h in payload["hypotheses"].items():
        lines.append(f"- **{hid}**: `{h.get('status')}`")
    lines.extend(["", "## Funnel families (IS-selected → OOS gate)", ""])
    for family in payload["families"]:
        chosen = family.get("selected_on_is") or {}
        spec = chosen.get("spec") or {}
        is_m = (chosen.get("metrics") or {}).get("is") or {}
        oos_m = (chosen.get("metrics") or {}).get("oos") or {}
        lines.append(
            f"- `{family['family']}` → `{spec.get('id')}` · "
            f"OOS **{family['oos_gate']['status']}** · "
            f"IS cycles={is_m.get('n_cycles')} α/cycle={is_m.get('alpha_per_cycle')} · "
            f"OOS cycles={oos_m.get('n_cycles')} α/cycle={oos_m.get('alpha_per_cycle')} "
            f"capture={oos_m.get('capture_pct')}%"
        )
    lines.extend(["", "## Orthogonality (selected masks)", ""])
    for row in payload["orthogonality"]:
        lines.append(f"- `{row['a']}` × `{row['b']}`: φ={row['phi']}")
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "- 參數只看 IS；OOS 不回頭調參。",
            "- W20 notUR ∧ W5 weak 視為一個結構包，不能假裝兩腿互相正交。",
            f"- {payload['market_stratum_note']}",
            "- Research only；未採納 Strategy / Order layer。",
            "",
        ]
    )
    return "\n".join(lines)


def write_artifacts(
    payload: dict[str, Any],
    *,
    out_dir: Path | None = None,
    stamp: str | None = None,
) -> dict[str, Path]:
    out = out_dir or (REPORTS_RESEARCH / "dual-branch-copytrade")
    out.mkdir(parents=True, exist_ok=True)
    prefix = stamp or date.today().strftime("%Y%m%d")
    stem = out / f"{prefix}_{FILE_STEM}"
    json_path = Path(str(stem) + ".json")
    md_path = Path(str(stem) + ".md")
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return {"json": json_path, "md": md_path}
