"""00981A 等 ETF 持股變化跟單回測 → copytrade_* SQLite tables。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from analytics.bench import (
    bench_close as _bench_close,
    bench_open as _bench_open,
    bench_return_entry_to_exit,
    compute_excess_significance,
)
from copytrade.signals import (
    ADD_ACTIONS,
    INITIATION_ACTION,
    REPEAT_ADD_ACTION,
    CopytradeSignal,
    filter_grouped_signals,
    group_signals_by_date,
    iter_copytrade_signals,
    snapshot_pairs,
)
from flow_returns import (
    DEFAULT_BETA,
    exit_close_date_from_entry,
    return_pct,
    stock_close,
    stock_open,
    trading_dates_after,
)
from holdings_research import TW_SPOT_CODE
from rank_stats import max_drawdown_pct
from report_paths import REPORTS_RESEARCH
from stock_db import (
    COPYTRADE_VERSION,
    load_stock_beta_map,
    persist_copytrade_bundle,
)
ACTION_FILTER_ALL_ADD = "all_add"
ACTION_FILTER_INITIATION = INITIATION_ACTION

ALLOCATION_EQUAL = "equal"
ALLOCATION_WEIGHT_PCT = "weight_pct"
ALLOCATION_MODES = frozenset({ALLOCATION_EQUAL, ALLOCATION_WEIGHT_PCT})
WEIGHT_PASS_MULT = 1.5
WEIGHT_FAIL_MULT = 0.5
DEFAULT_SIGNAL_CAPITAL_NTD = 10_000.0

# 舊代號（向後相容）→ 新矩陣 L{L}H{H}
LEGACY_STRATEGY_ALIASES: dict[str, str] = {
    "S0": "L1H1",
    "S1": "L2H1",
    "S2": "L3H1",
    "H1": "L1H2",
    "H5": "L1H5",
}


def _lag_label(entry_lag_days: int) -> str:
    if entry_lag_days < 0:
        return "L0"
    return f"L{entry_lag_days + 1}"


def _strategy_label(
    *,
    entry_lag_days: int,
    hold_trading_days: int,
    entry_price_mode: str,
) -> str:
    lag = _lag_label(entry_lag_days)
    if entry_lag_days < 0:
        day_desc = "T（經理人當天）"
    else:
        day_desc = f"T+{entry_lag_days + 1}"
    px = "收盤" if entry_price_mode == "close" else "開盤"
    if hold_trading_days == 1:
        hold_desc = "當日收盤賣出"
    else:
        hold_desc = f"持有 {hold_trading_days} 交易日收盤賣出"
    note = ""
    if entry_lag_days < 0 and entry_price_mode == "open":
        note = " · 開盤尚不知持股"
    elif entry_lag_days < 0 and entry_price_mode == "close":
        note = " · oracle／lookahead"
    return f"{lag} {day_desc} {px}買入、{hold_desc}{note}"


def build_matrix_strategies(
    *,
    include_l0: bool = True,
    max_hold: int = 5,
) -> tuple[dict, ...]:
    """L0O/L0C + L1–L3 × H1..max_hold。max_hold=5 → 25 格；max_hold=20 → 100 格。"""
    if max_hold < 1:
        raise ValueError("max_hold must be >= 1")
    specs: list[dict] = []
    if include_l0:
        for hold in range(1, max_hold + 1):
            for mode, sid_prefix in (("open", "L0O"), ("close", "L0C")):
                specs.append(
                    {
                        "strategy_id": f"{sid_prefix}-H{hold}",
                        "entry_lag_days": -1,
                        "hold_trading_days": hold,
                        "entry_price_mode": mode,
                    }
                )
    for entry_lag in range(3):
        lag_label = f"L{entry_lag + 1}"
        for hold in range(1, max_hold + 1):
            specs.append(
                {
                    "strategy_id": f"{lag_label}H{hold}",
                    "entry_lag_days": entry_lag,
                    "hold_trading_days": hold,
                    "entry_price_mode": "open",
                }
            )
    return tuple(
        {
            **s,
            "strategy_label": _strategy_label(
                entry_lag_days=int(s["entry_lag_days"]),
                hold_trading_days=int(s["hold_trading_days"]),
                entry_price_mode=str(s["entry_price_mode"]),
            ),
        }
        for s in specs
    )


MATRIX_STRATEGIES = build_matrix_strategies(include_l0=True, max_hold=5)

DEFAULT_STRATEGIES: tuple[dict, ...] = tuple(
    s for s in MATRIX_STRATEGIES if s["strategy_id"] in set(LEGACY_STRATEGY_ALIASES.values())
)


@dataclass
class CopytradeLegResult:
    signal_date: str
    stock_id: str
    stock_name: str
    action: str
    share_delta: float
    weight_delta: float | None
    entry_date: str | None
    exit_date: str | None
    entry_px: float | None
    exit_px: float | None
    allocated_ntd: float
    pnl_ntd: float
    return_pct: float
    gross_return_pct: float
    status: str


@dataclass
class CopytradeDayResult:
    signal_date: str
    entry_date: str | None
    exit_date: str | None
    n_legs: int
    deployed_ntd: float
    pnl_ntd: float
    return_pct: float
    bench_return_pct: float
    alpha_ntd: float
    capm_alpha_ntd: float
    portfolio_beta: float
    status: str
    legs: list[CopytradeLegResult] = field(default_factory=list)


@dataclass
class CopytradeRunResult:
    run_id: str
    etf_code: str
    strategy_id: str
    strategy_label: str
    capital_ntd: float
    entry_lag_days: int
    hold_trading_days: int
    entry_price_mode: str
    cost_bps: float
    window_start: str | None
    window_end: str | None
    signal_days: list[CopytradeDayResult]
    n_signal_days: int
    n_complete_days: int
    total_deployed_ntd: float
    total_pnl_ntd: float
    total_return_pct: float | None
    avg_day_return_pct: float | None
    win_rate_pct: float | None
    max_drawdown_pct: float | None
    total_bench_return_pct: float | None
    total_alpha_ntd: float
    total_capm_alpha_ntd: float
    mean_excess_pct: float | None
    p_value_ttest: float | None
    p_value_wilcoxon: float | None
    t_stat: float | None
    batch_id: str | None = None


def resolve_entry_date(
    conn: sqlite3.Connection,
    signal_date: str,
    entry_lag_days: int,
) -> str | None:
    if entry_lag_days < 0:
        return signal_date
    dates = trading_dates_after(conn, signal_date, count=entry_lag_days + 1)
    if len(dates) <= entry_lag_days:
        return None
    return dates[entry_lag_days]


def _entry_price(
    conn: sqlite3.Connection,
    stock_id: str,
    entry_date: str,
    entry_price_mode: str,
) -> float | None:
    if entry_price_mode == "close":
        return stock_close(conn, stock_id, entry_date)
    return stock_open(conn, stock_id, entry_date)


def _portfolio_beta(
    beta_map: dict[str, sqlite3.Row],
    legs: list[CopytradeLegResult],
) -> float:
    total = sum(lg.allocated_ntd for lg in legs if lg.allocated_ntd > 0)
    if total <= 0:
        return DEFAULT_BETA
    weighted = 0.0
    for lg in legs:
        if lg.allocated_ntd <= 0:
            continue
        row = beta_map.get(lg.stock_id)
        b = DEFAULT_BETA if row is None or row["beta"] is None else float(row["beta"])
        weighted += lg.allocated_ntd * b
    return weighted / total


def compute_win_rate_stats(
    day_results: list[CopytradeDayResult],
) -> dict[str, float | int | None]:
    complete = [d for d in day_results if d.status == "complete"]
    if not complete:
        return {
            "win_rate_gross_pct": None,
            "win_rate_vs_bench_pct": None,
            "win_rate_alpha_pct": None,
        }
    n = len(complete)
    gross = sum(1 for d in complete if d.pnl_ntd > 0)
    vs_bench = sum(1 for d in complete if d.return_pct > d.bench_return_pct)
    vs_alpha = sum(1 for d in complete if d.alpha_ntd > 0)
    return {
        "win_rate_gross_pct": round(gross / n * 100.0, 2),
        "win_rate_vs_bench_pct": round(vs_bench / n * 100.0, 2),
        "win_rate_alpha_pct": round(vs_alpha / n * 100.0, 2),
    }


def leg_allocations_ntd(
    legs_in: list[CopytradeSignal],
    capital_ntd: float,
    allocation_mode: str = ALLOCATION_EQUAL,
) -> list[float]:
    """equal：等權；weight_pct：依當日持股 weight_pct 在訊號 leg 間比例配置。"""
    if allocation_mode not in ALLOCATION_MODES:
        raise ValueError(f"unknown allocation_mode: {allocation_mode}")
    n = len(legs_in)
    if n <= 0:
        return []
    if allocation_mode == ALLOCATION_EQUAL or n == 1:
        per = capital_ntd / n
        return [per] * n
    weights = [max(float(sig.weight_pct_curr or 0), 0.0) for sig in legs_in]
    total = sum(weights)
    if total <= 0:
        per = capital_ntd / n
        return [per] * n
    return [capital_ntd * w / total for w in weights]


def leg_allocations_from_multipliers(
    multipliers: list[float],
    capital_ntd: float,
) -> list[float]:
    """依 leg 倍率配置，總和 = capital_ntd（R-A/R-B 加權復檢）。"""
    if not multipliers:
        return []
    total = sum(max(0.0, float(m)) for m in multipliers)
    if total <= 0:
        per = capital_ntd / len(multipliers)
        return [per] * len(multipliers)
    return [capital_ntd * max(0.0, float(m)) / total for m in multipliers]


def compute_leg(
    conn: sqlite3.Connection,
    sig: CopytradeSignal,
    *,
    entry_date: str,
    exit_date: str,
    allocated_ntd: float,
    cost_bps: float,
    entry_price_mode: str,
    entry_px_override: float | None = None,
) -> CopytradeLegResult:
    entry_px = (
        entry_px_override
        if entry_px_override is not None
        else _entry_price(conn, sig.stock_id, entry_date, entry_price_mode)
    )
    exit_px = stock_close(conn, sig.stock_id, exit_date)
    if entry_px is None or exit_px is None or entry_px <= 0:
        return CopytradeLegResult(
            signal_date=sig.signal_date,
            stock_id=sig.stock_id,
            stock_name=sig.stock_name,
            action=sig.action,
            share_delta=sig.share_delta,
            weight_delta=sig.weight_delta,
            entry_date=entry_date,
            exit_date=exit_date,
            entry_px=entry_px,
            exit_px=exit_px,
            allocated_ntd=allocated_ntd,
            pnl_ntd=0.0,
            return_pct=0.0,
            gross_return_pct=0.0,
            status="skip_no_prices",
        )
    gross = return_pct(entry_px, exit_px)
    net = gross - cost_bps / 100.0
    pnl = allocated_ntd * net / 100.0
    return CopytradeLegResult(
        signal_date=sig.signal_date,
        stock_id=sig.stock_id,
        stock_name=sig.stock_name,
        action=sig.action,
        share_delta=sig.share_delta,
        weight_delta=sig.weight_delta,
        entry_date=entry_date,
        exit_date=exit_date,
        entry_px=entry_px,
        exit_px=exit_px,
        allocated_ntd=allocated_ntd,
        pnl_ntd=pnl,
        return_pct=net,
        gross_return_pct=gross,
        status="complete",
    )


def compute_signal_day(
    conn: sqlite3.Connection,
    signal_date: str,
    legs_in: list[CopytradeSignal],
    *,
    capital_ntd: float,
    entry_lag_days: int,
    hold_trading_days: int,
    cost_bps: float,
    entry_price_mode: str,
    beta_map: dict[str, sqlite3.Row],
    allocation_mode: str = ALLOCATION_EQUAL,
    entry_px_overrides: dict[tuple[str, str], float] | None = None,
    leg_multipliers: list[float] | None = None,
) -> CopytradeDayResult:
    if not legs_in:
        return CopytradeDayResult(
            signal_date=signal_date,
            entry_date=None,
            exit_date=None,
            n_legs=0,
            deployed_ntd=0.0,
            pnl_ntd=0.0,
            return_pct=0.0,
            bench_return_pct=0.0,
            alpha_ntd=0.0,
            capm_alpha_ntd=0.0,
            portfolio_beta=DEFAULT_BETA,
            status="skip_no_legs",
        )

    entry_date = resolve_entry_date(conn, signal_date, entry_lag_days)
    if entry_date is None:
        return CopytradeDayResult(
            signal_date=signal_date,
            entry_date=None,
            exit_date=None,
            n_legs=len(legs_in),
            deployed_ntd=0.0,
            pnl_ntd=0.0,
            return_pct=0.0,
            bench_return_pct=0.0,
            alpha_ntd=0.0,
            capm_alpha_ntd=0.0,
            portfolio_beta=DEFAULT_BETA,
            status="skip_no_entry_date",
        )

    exit_date = exit_close_date_from_entry(conn, entry_date, hold_trading_days)
    if exit_date is None:
        return CopytradeDayResult(
            signal_date=signal_date,
            entry_date=entry_date,
            exit_date=None,
            n_legs=len(legs_in),
            deployed_ntd=0.0,
            pnl_ntd=0.0,
            return_pct=0.0,
            bench_return_pct=0.0,
            alpha_ntd=0.0,
            capm_alpha_ntd=0.0,
            portfolio_beta=DEFAULT_BETA,
            status="skip_no_exit_date",
        )

    if leg_multipliers is not None:
        if len(leg_multipliers) != len(legs_in):
            raise ValueError("leg_multipliers length must match legs_in")
        allocs = leg_allocations_from_multipliers(leg_multipliers, capital_ntd)
    else:
        allocs = leg_allocations_ntd(legs_in, capital_ntd, allocation_mode)
    overrides = entry_px_overrides or {}
    leg_results = [
        compute_leg(
            conn,
            sig,
            entry_date=entry_date,
            exit_date=exit_date,
            allocated_ntd=alloc_ntd,
            cost_bps=cost_bps,
            entry_price_mode=entry_price_mode,
            entry_px_override=overrides.get((sig.signal_date, sig.stock_id)),
        )
        for sig, alloc_ntd in zip(legs_in, allocs)
    ]
    priced = [lg for lg in leg_results if lg.status == "complete"]
    if not priced:
        return CopytradeDayResult(
            signal_date=signal_date,
            entry_date=entry_date,
            exit_date=exit_date,
            n_legs=len(legs_in),
            deployed_ntd=0.0,
            pnl_ntd=0.0,
            return_pct=0.0,
            bench_return_pct=0.0,
            alpha_ntd=0.0,
            capm_alpha_ntd=0.0,
            portfolio_beta=DEFAULT_BETA,
            status="skip_no_prices",
            legs=leg_results,
        )

    deployed = sum(lg.allocated_ntd for lg in priced)
    pnl = sum(lg.pnl_ntd for lg in priced)
    ret_pct = pnl / deployed * 100.0 if deployed > 0 else 0.0
    bench_ret = bench_return_entry_to_exit(
        conn,
        entry_date,
        exit_date,
        entry_price_mode=entry_price_mode,
    )
    if bench_ret is None:
        bench_ret = 0.0
    bench_pnl = deployed * bench_ret / 100.0
    port_beta = _portfolio_beta(beta_map, priced)
    capm_bench_pnl = deployed * port_beta * bench_ret / 100.0
    return CopytradeDayResult(
        signal_date=signal_date,
        entry_date=entry_date,
        exit_date=exit_date,
        n_legs=len(priced),
        deployed_ntd=deployed,
        pnl_ntd=pnl,
        return_pct=ret_pct,
        bench_return_pct=bench_ret,
        alpha_ntd=pnl - bench_pnl,
        capm_alpha_ntd=pnl - capm_bench_pnl,
        portfolio_beta=port_beta,
        status="complete",
        legs=leg_results,
    )


def run_copytrade_backtest(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    strategy_id: str,
    strategy_label: str,
    entry_lag_days: int,
    hold_trading_days: int,
    entry_price_mode: str = "open",
    capital_ntd: float = 10_000.0,
    cost_bps: float = 0.0,
    window_start: str | None = None,
    window_end: str | None = None,
    run_id: str | None = None,
    run_suffix: str | None = None,
    batch_id: str | None = None,
    grouped: dict[str, list[CopytradeSignal]] | None = None,
    beta_map: dict[str, sqlite3.Row] | None = None,
    allocation_mode: str = ALLOCATION_EQUAL,
) -> CopytradeRunResult:
    if grouped is None:
        signals = iter_copytrade_signals(
            conn, etf_code, window_start=window_start, window_end=window_end
        )
        grouped = group_signals_by_date(signals)
    if beta_map is None:
        beta_map, _ = load_stock_beta_map(conn)

    day_results: list[CopytradeDayResult] = []
    for signal_date in sorted(grouped):
        day_results.append(
            compute_signal_day(
                conn,
                signal_date,
                grouped[signal_date],
                capital_ntd=capital_ntd,
                entry_lag_days=entry_lag_days,
                hold_trading_days=hold_trading_days,
                cost_bps=cost_bps,
                entry_price_mode=entry_price_mode,
                beta_map=beta_map,
                allocation_mode=allocation_mode,
            )
        )

    complete = [d for d in day_results if d.status == "complete"]
    total_deployed = sum(d.deployed_ntd for d in complete)
    total_pnl = sum(d.pnl_ntd for d in complete)
    total_ret = total_pnl / total_deployed * 100.0 if total_deployed > 0 else None
    day_returns = [d.return_pct for d in complete]
    avg_day = sum(day_returns) / len(day_returns) if day_returns else None
    wins = sum(1 for d in complete if d.pnl_ntd > 0)
    win_rate = wins / len(complete) * 100.0 if complete else None
    mdd = max_drawdown_pct(day_returns)
    total_bench = sum(d.bench_return_pct for d in complete)
    total_alpha = sum(d.alpha_ntd for d in complete)
    total_capm_alpha = sum(d.capm_alpha_ntd for d in complete)
    sig = compute_excess_significance(day_results)

    w_start = window_start or (min(grouped) if grouped else None)
    w_end = window_end or (max(grouped) if grouped else None)
    suffix = run_suffix or date.today().strftime("%Y%m%d")
    rid = run_id or f"{etf_code.lower()}-copytrade-{strategy_id}-{suffix}"

    return CopytradeRunResult(
        run_id=rid,
        etf_code=etf_code,
        strategy_id=strategy_id,
        strategy_label=strategy_label,
        capital_ntd=capital_ntd,
        entry_lag_days=entry_lag_days,
        hold_trading_days=hold_trading_days,
        entry_price_mode=entry_price_mode,
        cost_bps=cost_bps,
        window_start=w_start,
        window_end=w_end,
        signal_days=day_results,
        n_signal_days=len(day_results),
        n_complete_days=len(complete),
        total_deployed_ntd=total_deployed,
        total_pnl_ntd=total_pnl,
        total_return_pct=round(total_ret, 4) if total_ret is not None else None,
        avg_day_return_pct=round(avg_day, 4) if avg_day is not None else None,
        win_rate_pct=round(win_rate, 2) if win_rate is not None else None,
        max_drawdown_pct=mdd,
        total_bench_return_pct=round(total_bench, 4) if complete else None,
        total_alpha_ntd=round(total_alpha, 2),
        total_capm_alpha_ntd=round(total_capm_alpha, 2),
        mean_excess_pct=sig["mean_excess_pct"],
        p_value_ttest=sig["p_value_ttest"],
        p_value_wilcoxon=sig["p_value_wilcoxon"],
        t_stat=sig["t_stat"],
        batch_id=batch_id,
    )


def persist_copytrade_run(conn: sqlite3.Connection, result: CopytradeRunResult) -> str:
    signal_rows = [
        {
            "signal_date": d.signal_date,
            "entry_date": d.entry_date,
            "exit_date": d.exit_date,
            "n_legs": d.n_legs,
            "deployed_ntd": d.deployed_ntd,
            "pnl_ntd": d.pnl_ntd,
            "return_pct": d.return_pct,
            "bench_return_pct": d.bench_return_pct,
            "alpha_ntd": d.alpha_ntd,
            "capm_alpha_ntd": d.capm_alpha_ntd,
            "portfolio_beta": d.portfolio_beta,
            "status": d.status,
        }
        for d in result.signal_days
    ]
    leg_rows: list[dict] = []
    for d in result.signal_days:
        for lg in d.legs:
            leg_rows.append(
                {
                    "signal_date": lg.signal_date,
                    "stock_id": lg.stock_id,
                    "stock_name": lg.stock_name,
                    "action": lg.action,
                    "share_delta": lg.share_delta,
                    "weight_delta": lg.weight_delta,
                    "entry_date": lg.entry_date,
                    "exit_date": lg.exit_date,
                    "entry_px": lg.entry_px,
                    "exit_px": lg.exit_px,
                    "allocated_ntd": lg.allocated_ntd,
                    "pnl_ntd": lg.pnl_ntd,
                    "return_pct": lg.return_pct,
                    "gross_return_pct": lg.gross_return_pct,
                    "status": lg.status,
                }
            )
    return persist_copytrade_bundle(
        conn,
        run_row={
            "run_id": result.run_id,
            "etf_code": result.etf_code,
            "strategy_id": result.strategy_id,
            "strategy_label": result.strategy_label,
            "capital_ntd": result.capital_ntd,
            "entry_lag_days": result.entry_lag_days,
            "hold_trading_days": result.hold_trading_days,
            "entry_price_mode": result.entry_price_mode,
            "cost_bps": result.cost_bps,
            "window_start": result.window_start,
            "window_end": result.window_end,
            "copytrade_version": COPYTRADE_VERSION,
            "n_signal_days": result.n_signal_days,
            "n_complete_days": result.n_complete_days,
            "total_deployed_ntd": result.total_deployed_ntd,
            "total_pnl_ntd": result.total_pnl_ntd,
            "total_return_pct": result.total_return_pct,
            "avg_day_return_pct": result.avg_day_return_pct,
            "win_rate_pct": result.win_rate_pct,
            "max_drawdown_pct": result.max_drawdown_pct,
            "total_bench_return_pct": result.total_bench_return_pct,
            "total_alpha_ntd": result.total_alpha_ntd,
            "total_capm_alpha_ntd": result.total_capm_alpha_ntd,
            "mean_excess_pct": result.mean_excess_pct,
            "p_value_ttest": result.p_value_ttest,
            "p_value_wilcoxon": result.p_value_wilcoxon,
            "t_stat": result.t_stat,
            "batch_id": result.batch_id,
            "message": None,
        },
        signal_days=signal_rows,
        legs=leg_rows,
    )


def resolve_strategy_specs(
    strategy_arg: str,
    *,
    matrix: bool,
    include_l0: bool,
    max_hold: int = 5,
) -> tuple[dict, ...]:
    if matrix:
        return build_matrix_strategies(include_l0=include_l0, max_hold=max_hold)
    if strategy_arg.lower() == "all":
        specs = build_matrix_strategies(include_l0=include_l0, max_hold=max_hold)
        return specs
    wanted = {s.strip().upper() for s in strategy_arg.split(",") if s.strip()}
    expanded = set(wanted)
    for alias, target in LEGACY_STRATEGY_ALIASES.items():
        if alias in wanted:
            expanded.add(target)
    pool = build_matrix_strategies(include_l0=True, max_hold=max(max_hold, 20))
    return tuple(s for s in pool if s["strategy_id"] in expanded)


def build_horizon_decay_rows(
    results: list[CopytradeRunResult],
    etf_code: str,
) -> list[dict]:
    rows: list[dict] = []
    for r in results:
        entry_row = _matrix_row_key(r.strategy_id)
        horizon = _matrix_col_key(r.strategy_id)
        if entry_row is None or horizon is None:
            continue
        rows.append(
            {
                "etf_code": etf_code,
                "entry_row": entry_row,
                "horizon": horizon,
                "strategy_id": r.strategy_id,
                "run_id": r.run_id,
                "n_complete": r.n_complete_days,
                "total_pnl_ntd": r.total_pnl_ntd,
                "total_alpha_ntd": r.total_alpha_ntd,
                "total_capm_alpha_ntd": r.total_capm_alpha_ntd,
                "mean_excess_pct": r.mean_excess_pct,
                "p_value_ttest": r.p_value_ttest,
                "p_value_wilcoxon": r.p_value_wilcoxon,
                "t_stat": r.t_stat,
            }
        )
    return rows


def summarize_decay_insights(
    decay_rows: list[dict],
    entry_row: str = "L1",
    *,
    alpha: float = 0.05,
) -> dict[str, object]:
    """α 峰值、首次/末次顯著 H（Wilcoxon p<α）。"""
    rows = sorted(
        [r for r in decay_rows if r["entry_row"] == entry_row],
        key=lambda r: int(r["horizon"]),
    )
    if not rows:
        return {}

    def _p(r: dict) -> float | None:
        v = r.get("p_value_wilcoxon")
        return float(v) if v is not None else None

    peak = max(rows, key=lambda r: float(r["total_alpha_ntd"] or 0))
    first_sig: int | None = None
    last_sig: int | None = None
    for r in rows:
        p = _p(r)
        if p is not None and p < alpha:
            first_sig = int(r["horizon"])
            break
    for r in reversed(rows):
        p = _p(r)
        if p is not None and p < alpha:
            last_sig = int(r["horizon"])
            break
    all_insignificant = all(
        (_p(r) is None or _p(r) > alpha) for r in rows  # type: ignore[operator]
    )
    return {
        "entry_row": entry_row,
        "peak_h": int(peak["horizon"]),
        "peak_alpha_ntd": peak["total_alpha_ntd"],
        "first_significant_h": first_sig,
        "last_significant_h": last_sig,
        "all_horizons_insignificant": all_insignificant,
    }


def count_hold_trading_days(
    conn: sqlite3.Connection,
    entry_date: str,
    exit_date: str,
) -> int:
    """進場日至出場日（含）的交易日數。"""
    if not entry_date or not exit_date or exit_date < entry_date:
        return 0
    cur = entry_date
    count = 0
    while cur <= exit_date:
        count += 1
        if cur == exit_date:
            break
        nxt = trading_dates_after(conn, cur, count=1)
        if not nxt:
            break
        cur = nxt[0]
    return count


def _paired_allocation_diffs(
    equal_days: list[CopytradeDayResult],
    weight_days: list[CopytradeDayResult],
) -> dict[str, list[float]]:
    by_eq = {d.signal_date: d for d in equal_days if d.status == "complete"}
    by_wt = {d.signal_date: d for d in weight_days if d.status == "complete"}
    dates = sorted(set(by_eq) & set(by_wt))
    return {
        "return_pct": [by_wt[d].return_pct - by_eq[d].return_pct for d in dates],
        "alpha_ntd": [by_wt[d].alpha_ntd - by_eq[d].alpha_ntd for d in dates],
        "pnl_ntd": [by_wt[d].pnl_ntd - by_eq[d].pnl_ntd for d in dates],
        "dates": dates,
    }


def _paired_significance(diffs: list[float]) -> dict[str, float | None]:
    if len(diffs) < 3:
        return {"mean_diff": None, "p_value_ttest": None, "p_value_wilcoxon": None, "t_stat": None}
    mean_diff = sum(diffs) / len(diffs)
    try:
        from scipy.stats import ttest_1samp, wilcoxon

        t_stat, p_t = ttest_1samp(diffs, 0.0)
        non_zero = [d for d in diffs if abs(d) > 1e-12]
        if len(non_zero) >= 3:
            _, p_w = wilcoxon(non_zero)
        else:
            p_w = None
    except Exception:
        return {
            "mean_diff": round(mean_diff, 6),
            "p_value_ttest": None,
            "p_value_wilcoxon": None,
            "t_stat": None,
        }
    return {
        "mean_diff": round(mean_diff, 6),
        "p_value_ttest": round(float(p_t), 4) if p_t == p_t else None,
        "p_value_wilcoxon": round(float(p_w), 4) if p_w is not None and p_w == p_w else None,
        "t_stat": round(float(t_stat), 4) if t_stat == t_stat else None,
    }


# §10.1 Primary：每訊號日各 10k、持倉可重疊（無約束累計 α）
PRIMARY_ALPHA_FIELD = "total_alpha_ntd"
SECONDARY_ALPHA_FIELD = "recycled_total_alpha_ntd"


def primary_alpha_ntd(summary: dict) -> float:
    return float(summary.get(PRIMARY_ALPHA_FIELD) or 0)


def primary_alpha_improved(summary: dict, base: dict) -> bool:
    return primary_alpha_ntd(summary) > primary_alpha_ntd(base)


def _summarize_day_run(
    day_results: list[CopytradeDayResult],
    conn: sqlite3.Connection,
) -> dict[str, float | int | None]:
    complete = [d for d in day_results if d.status == "complete"]
    multi = [d for d in complete if d.n_legs > 1]
    sig = compute_excess_significance(day_results)
    sim = simulate_capital_recycling(
        conn,
        [
            {
                "signal_date": d.signal_date,
                "entry_date": d.entry_date,
                "exit_date": d.exit_date,
                "alpha_ntd": d.alpha_ntd,
                "pnl_ntd": d.pnl_ntd,
                "status": d.status,
            }
            for d in day_results
        ],
    )
    day_returns = [d.return_pct for d in complete]
    avg_day = sum(day_returns) / len(day_returns) if day_returns else None
    win = compute_win_rate_stats(day_results)
    n_legs = sum(d.n_legs for d in complete)
    return {
        "n_complete_days": len(complete),
        "n_multi_leg_days": len(multi),
        "n_legs": n_legs,
        "total_pnl_ntd": round(sum(d.pnl_ntd for d in complete), 2),
        "total_alpha_ntd": round(sum(d.alpha_ntd for d in complete), 2),
        "avg_day_return_pct": round(avg_day, 4) if avg_day is not None else None,
        "recycled_n_cycles": sim["recycled_n_cycles"],
        "recycled_total_alpha_ntd": sim["recycled_total_alpha_ntd"],
        "recycled_total_pnl_ntd": sim["recycled_total_pnl_ntd"],
        "mean_excess_pct": sig.get("mean_excess_pct"),
        "p_value_ttest": sig.get("p_value_ttest"),
        "p_value_wilcoxon": sig.get("p_value_wilcoxon"),
        **win,
    }


def _summarize_allocation_run(
    day_results: list[CopytradeDayResult],
    conn: sqlite3.Connection,
) -> dict[str, float | int | None]:
    return _summarize_day_run(day_results, conn)


def run_allocation_comparison(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    strategy_id: str = "L1H9",
    entry_lag_days: int = 0,
    hold_trading_days: int = 9,
    entry_price_mode: str = "open",
    capital_ntd: float = 10_000.0,
    cost_bps: float = 0.0,
    window_start: str | None = None,
    window_end: str | None = None,
    batch_id: str | None = None,
    persist: bool = True,
) -> dict[str, object]:
    """等權 vs 依 weight_pct 配置；其餘參數（L/H/窗口）固定。"""
    from stock_db import persist_copytrade_allocation_compare, persist_copytrade_research_conclusions

    col = _matrix_col_key(strategy_id)
    row = _matrix_row_key(strategy_id)
    if col is not None:
        hold_trading_days = col
    if row == "L1":
        entry_lag_days = 0
    elif row == "L2":
        entry_lag_days = 1
    elif row == "L3":
        entry_lag_days = 2

    signals = iter_copytrade_signals(
        conn, etf_code, window_start=window_start, window_end=window_end
    )
    grouped = group_signals_by_date(signals)
    beta_map, _ = load_stock_beta_map(conn)

    equal_days: list[CopytradeDayResult] = []
    weight_days: list[CopytradeDayResult] = []
    for signal_date in sorted(grouped):
        kwargs = dict(
            capital_ntd=capital_ntd,
            entry_lag_days=entry_lag_days,
            hold_trading_days=hold_trading_days,
            cost_bps=cost_bps,
            entry_price_mode=entry_price_mode,
            beta_map=beta_map,
        )
        equal_days.append(
            compute_signal_day(
                conn, signal_date, grouped[signal_date], allocation_mode=ALLOCATION_EQUAL, **kwargs
            )
        )
        weight_days.append(
            compute_signal_day(
                conn,
                signal_date,
                grouped[signal_date],
                allocation_mode=ALLOCATION_WEIGHT_PCT,
                **kwargs,
            )
        )

    eq_sum = _summarize_allocation_run(equal_days, conn)
    wt_sum = _summarize_allocation_run(weight_days, conn)
    paired = _paired_allocation_diffs(equal_days, weight_days)
    sig_ret = _paired_significance(paired["return_pct"])
    sig_alpha = _paired_significance(paired["alpha_ntd"])

    bid = batch_id or (
        f"{etf_code.lower()}-allocation-compare-{strategy_id.lower()}-"
        f"{date.today().strftime('%Y%m%d')}"
    )

    rows = []
    for mode, summary in (
        (ALLOCATION_EQUAL, eq_sum),
        (ALLOCATION_WEIGHT_PCT, wt_sum),
    ):
        rows.append(
            {
                "etf_code": etf_code,
                "strategy_id": strategy_id,
                "allocation_mode": mode,
                "capital_ntd": capital_ntd,
                "entry_lag_days": entry_lag_days,
                "hold_trading_days": hold_trading_days,
                **summary,
            }
        )

    better = (
        ALLOCATION_WEIGHT_PCT
        if (wt_sum["total_alpha_ntd"] or 0) > (eq_sum["total_alpha_ntd"] or 0)
        else ALLOCATION_EQUAL
    )
    alpha_diff = (wt_sum["total_alpha_ntd"] or 0) - (eq_sum["total_alpha_ntd"] or 0)
    recycled_diff = (wt_sum["recycled_total_alpha_ntd"] or 0) - (
        eq_sum["recycled_total_alpha_ntd"] or 0
    )
    sig_label = "無顯著差異"
    p_w = sig_alpha.get("p_value_wilcoxon")
    if p_w is not None:
        sig_label = "顯著" if p_w < 0.05 else "無顯著差異"

    conclusion = (
        f"{strategy_id}：等權 vs 按 weight_pct 配置（總資金 {capital_ntd:,.0f} NTD/訊號）。"
        f"無約束累計 α：等權 {eq_sum['total_alpha_ntd']:+,.0f}、"
        f"按比例 {wt_sum['total_alpha_ntd']:+,.0f}（差 {alpha_diff:+,.0f}）。"
        f"單池回收 α：等權 {eq_sum['recycled_total_alpha_ntd']:+,.0f}、"
        f"按比例 {wt_sum['recycled_total_alpha_ntd']:+,.0f}（差 {recycled_diff:+,.0f}）。"
        f"配對檢定（weight−equal 日均超額%）：p(W)={p_w} → **{sig_label}**。"
        f"多檔訊號日 {eq_sum['n_multi_leg_days']}/{eq_sum['n_complete_days']}。"
    )

    if persist:
        persist_copytrade_allocation_compare(conn, bid, rows)
        persist_copytrade_research_conclusions(
            conn,
            bid,
            [
                {
                    "etf_code": etf_code,
                    "analysis_type": "allocation_compare",
                    "entry_row": row,
                    "metric_key": "equal_vs_weight_pct",
                    "horizon": hold_trading_days,
                    "metric_value": alpha_diff,
                    "conclusion_zh": conclusion,
                    "details_json": __import__("json").dumps(
                        {
                            "equal": eq_sum,
                            "weight_pct": wt_sum,
                            "paired_return": sig_ret,
                            "paired_alpha_ntd": sig_alpha,
                            "better_total_alpha": better,
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
            replace_types=("allocation_compare",),
        )

    return {
        "batch_id": bid,
        "strategy_id": strategy_id,
        "equal": eq_sum,
        "weight_pct": wt_sum,
        "paired_return": sig_ret,
        "paired_alpha_ntd": sig_alpha,
        "conclusion_zh": conclusion,
        "better_total_alpha": better,
    }


def _resolve_matrix_strategy_params(
    strategy_id: str,
    *,
    entry_lag_days: int,
    hold_trading_days: int,
) -> tuple[int, int]:
    col = _matrix_col_key(strategy_id)
    row = _matrix_row_key(strategy_id)
    hold = col if col is not None else hold_trading_days
    lag = entry_lag_days
    if row == "L1":
        lag = 0
    elif row == "L2":
        lag = 1
    elif row == "L3":
        lag = 2
    return lag, hold


def _build_day_results(
    conn: sqlite3.Connection,
    grouped: dict[str, list[CopytradeSignal]],
    *,
    capital_ntd: float,
    entry_lag_days: int,
    hold_trading_days: int,
    cost_bps: float,
    entry_price_mode: str,
    beta_map: dict[str, sqlite3.Row],
    allocation_mode: str = ALLOCATION_EQUAL,
    entry_px_overrides: dict[tuple[str, str], float] | None = None,
    leg_multiplier_fn: object | None = None,
) -> list[CopytradeDayResult]:
    out: list[CopytradeDayResult] = []
    kwargs = dict(
        capital_ntd=capital_ntd,
        entry_lag_days=entry_lag_days,
        hold_trading_days=hold_trading_days,
        cost_bps=cost_bps,
        entry_price_mode=entry_price_mode,
        beta_map=beta_map,
        allocation_mode=allocation_mode,
        entry_px_overrides=entry_px_overrides,
    )
    for signal_date in sorted(grouped):
        legs = grouped[signal_date]
        leg_mults = None
        if leg_multiplier_fn is not None:
            leg_mults = [float(leg_multiplier_fn(sig)) for sig in legs]
        out.append(
            compute_signal_day(
                conn,
                signal_date,
                legs,
                leg_multipliers=leg_mults,
                **kwargs,
            )
        )
    return out


def _paired_action_diffs(
    baseline_days: list[CopytradeDayResult],
    filtered_days: list[CopytradeDayResult],
) -> dict[str, list[float]]:
    """僅在 filtered 有 complete 的訊號日上，比較兩策略同日損益。"""
    by_base = {d.signal_date: d for d in baseline_days if d.status == "complete"}
    by_filt = {d.signal_date: d for d in filtered_days if d.status == "complete"}
    dates = sorted(by_filt)
    return {
        "return_pct": [by_filt[d].return_pct - by_base[d].return_pct for d in dates],
        "alpha_ntd": [by_filt[d].alpha_ntd - by_base[d].alpha_ntd for d in dates],
        "pnl_ntd": [by_filt[d].pnl_ntd - by_base[d].pnl_ntd for d in dates],
        "dates": dates,
    }


def compute_overnight_gap_record(
    conn: sqlite3.Connection,
    etf_code: str,
    sig: CopytradeSignal,
    *,
    entry_lag_days: int,
) -> dict[str, object]:
    entry_date = resolve_entry_date(conn, sig.signal_date, entry_lag_days)
    signal_close = stock_close(conn, sig.stock_id, sig.signal_date)
    entry_open = (
        stock_open(conn, sig.stock_id, entry_date) if entry_date else None
    )
    if entry_date is None:
        status = "skip_no_entry_date"
    elif signal_close is None or signal_close <= 0:
        status = "missing_signal_close"
    elif entry_open is None or entry_open <= 0:
        status = "missing_entry_open"
    else:
        status = "complete"
    gap = (
        return_pct(signal_close, entry_open)
        if status == "complete"
        else None
    )
    return {
        "etf_code": etf_code,
        "signal_date": sig.signal_date,
        "stock_id": sig.stock_id,
        "entry_lag_days": entry_lag_days,
        "entry_date": entry_date,
        "signal_close": signal_close,
        "entry_open": entry_open,
        "overnight_gap_pct": round(gap, 4) if gap is not None else None,
        "status": status,
    }


def backfill_copytrade_overnight_gaps(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    entry_lag_days: int = 0,
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict[str, int]:
    """補齊 copytrade_leg_overnight_gaps（T 收盤 vs T+1 開盤）。"""
    from stock_db import persist_copytrade_overnight_gaps

    signals = iter_copytrade_signals(
        conn, etf_code, window_start=window_start, window_end=window_end
    )
    seen: set[tuple[str, str]] = set()
    rows: list[dict] = []
    for sig in signals:
        key = (sig.signal_date, sig.stock_id)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            compute_overnight_gap_record(
                conn, etf_code, sig, entry_lag_days=entry_lag_days
            )
        )
    n = persist_copytrade_overnight_gaps(conn, rows)
    complete = sum(1 for r in rows if r["status"] == "complete")
    return {
        "n_rows": n,
        "n_complete": complete,
        "n_missing": n - complete,
    }


def load_overnight_gap_map(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    entry_lag_days: int = 0,
) -> dict[tuple[str, str], float]:
    from stock_db import load_copytrade_overnight_gaps

    rows = load_copytrade_overnight_gaps(
        conn, etf_code, entry_lag_days=entry_lag_days, status="complete"
    )
    return {
        (str(r["signal_date"]), str(r["stock_id"])): float(r["overnight_gap_pct"])
        for r in rows
        if r["overnight_gap_pct"] is not None
    }


def _load_bars_asof(
    conn: sqlite3.Connection,
    stock_id: str,
    as_of_date: str,
    *,
    limit: int = 280,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT trade_date, open, high, low, close, volume
        FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' AND trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (stock_id, as_of_date, limit),
    ).fetchall()


def _build_leg_ta_snapshot_row(
    etf_code: str,
    sig: CopytradeSignal,
    tech: object,
    *,
    overextended_thresh: float,
    flow_score: float,
    chip_score: float,
) -> dict[str, object]:
    from score_engine import (
        classify_entry_context,
        has_strong_trend,
        is_overextended_without_strong_trend,
    )

    entry_ctx = classify_entry_context(
        tech,
        net_side="add",
        flow_score=flow_score,
        chip_score=chip_score,
        overextended_min=overextended_thresh,
    )
    uptrend_pullback = int(
        tech.dist_ma60_pct is not None
        and tech.dist_ma60_pct > 0
        and tech.dist_ma20_pct is not None
        and -8.0 <= tech.dist_ma20_pct <= 5.0
    )
    above_ma60 = int(tech.dist_ma60_pct is not None and tech.dist_ma60_pct > 0)
    return {
        "etf_code": etf_code,
        "signal_date": sig.signal_date,
        "stock_id": sig.stock_id,
        "entry_pattern": entry_ctx.signal,
        "entry_tags_json": json.dumps(list(entry_ctx.tags), ensure_ascii=False),
        "above_ma60": above_ma60,
        "uptrend_pullback": uptrend_pullback,
        "skip_overextended": int(is_overextended_without_strong_trend(entry_ctx)),
        "has_strong_trend": int(
            has_strong_trend(
                tech,
                flow_score=flow_score,
                chip_score=chip_score,
                overextended_min=overextended_thresh,
            )
        ),
        "dist_ma20_pct": tech.dist_ma20_pct,
        "dist_ma60_pct": tech.dist_ma60_pct,
        "position_52w_pct": tech.position_52w_pct,
        "overextended_thresh_pct": round(overextended_thresh, 2),
        "status": "complete",
    }


def backfill_copytrade_leg_ta_snapshots(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict[str, int]:
    """補齊 copytrade_leg_ta_snapshots（訊號日技術面 · 供 H2 動量假說研究）。"""
    from score_engine import NEUTRAL_SUBSCORE, chip_subscore_for_stock, extension_pct, overextended_min_pct
    from stock_context import MA20_DAYS, _compute_technical_from_rows
    from stock_db import load_copytrade_leg_ta_snapshots, persist_copytrade_leg_ta_snapshots

    existing = {
        (str(r["signal_date"]), str(r["stock_id"]))
        for r in load_copytrade_leg_ta_snapshots(conn, etf_code, status=None)
    }
    pending: list[CopytradeSignal] = []
    seen: set[tuple[str, str]] = set()
    for sig in iter_copytrade_signals(
        conn, etf_code, window_start=window_start, window_end=window_end
    ):
        key = (sig.signal_date, sig.stock_id)
        if key in seen or key in existing:
            continue
        seen.add(key)
        pending.append(sig)

    tech_by_key: dict[tuple[str, str], object] = {}
    extensions: list[float] = []
    for sig in pending:
        bars = _load_bars_asof(conn, sig.stock_id, sig.signal_date)
        if len(bars) < MA20_DAYS:
            continue
        tech = _compute_technical_from_rows(bars, entity_id=sig.stock_id)
        if tech is None:
            continue
        tech_by_key[(sig.signal_date, sig.stock_id)] = tech
        ext = extension_pct(tech)
        if ext is not None:
            extensions.append(ext)

    thresh = overextended_min_pct(extensions) if extensions else 12.0
    etf_codes = (etf_code,)
    rows: list[dict[str, object]] = []
    for sig in pending:
        key = (sig.signal_date, sig.stock_id)
        tech = tech_by_key.get(key)
        if tech is None:
            rows.append(
                {
                    "etf_code": etf_code,
                    "signal_date": sig.signal_date,
                    "stock_id": sig.stock_id,
                    "entry_pattern": None,
                    "entry_tags_json": "[]",
                    "above_ma60": 0,
                    "uptrend_pullback": 0,
                    "skip_overextended": 0,
                    "has_strong_trend": 0,
                    "dist_ma20_pct": None,
                    "dist_ma60_pct": None,
                    "position_52w_pct": None,
                    "overextended_thresh_pct": round(thresh, 2),
                    "status": "missing_bars",
                }
            )
            continue
        chip_score, _ = chip_subscore_for_stock(conn, sig.stock_id, etf_codes, {})
        rows.append(
            _build_leg_ta_snapshot_row(
                etf_code,
                sig,
                tech,
                overextended_thresh=thresh,
                flow_score=NEUTRAL_SUBSCORE,
                chip_score=chip_score,
            )
        )

    n = persist_copytrade_leg_ta_snapshots(conn, rows)
    complete = sum(1 for r in rows if r["status"] == "complete")
    return {
        "n_rows": n,
        "n_complete": complete,
        "n_missing": n - complete,
        "n_skipped_existing": len(existing),
    }


def load_ta_pattern_map(
    conn: sqlite3.Connection,
    etf_code: str,
) -> dict[tuple[str, str], dict[str, object]]:
    from stock_db import load_copytrade_leg_ta_snapshots

    rows = load_copytrade_leg_ta_snapshots(conn, etf_code, status="complete")
    return {
        (str(r["signal_date"]), str(r["stock_id"])): {
            "entry_pattern": r["entry_pattern"],
            "above_ma60": int(r["above_ma60"] or 0),
            "uptrend_pullback": int(r["uptrend_pullback"] or 0),
            "skip_overextended": int(r["skip_overextended"] or 0),
            "has_strong_trend": int(r["has_strong_trend"] or 0),
            "dist_ma20_pct": r["dist_ma20_pct"],
            "dist_ma60_pct": r["dist_ma60_pct"],
            "position_52w_pct": r["position_52w_pct"],
        }
        for r in rows
    }


def filter_grouped_by_ta(
    grouped: dict[str, list[CopytradeSignal]],
    ta_map: dict[tuple[str, str], dict[str, object]],
    predicate: Callable[[dict[str, object]], bool],
) -> dict[str, list[CopytradeSignal]]:
    out: dict[str, list[CopytradeSignal]] = {}
    for signal_date, legs in grouped.items():
        kept: list[CopytradeSignal] = []
        for sig in legs:
            snap = ta_map.get((signal_date, sig.stock_id))
            if snap is None or not predicate(snap):
                continue
            kept.append(sig)
        if kept:
            out[signal_date] = kept
    return out


def _two_proportion_ztest(
    success_a: int,
    n_a: int,
    success_b: int,
    n_b: int,
) -> dict[str, float | None]:
    if n_a < 1 or n_b < 1:
        return {"z_stat": None, "p_value": None, "rate_a": None, "rate_b": None}
    rate_a = success_a / n_a
    rate_b = success_b / n_b
    p_pool = (success_a + success_b) / (n_a + n_b)
    se = (p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b)) ** 0.5
    if se <= 0:
        return {
            "z_stat": None,
            "p_value": None,
            "rate_a": round(rate_a * 100, 2),
            "rate_b": round(rate_b * 100, 2),
        }
    z = (rate_a - rate_b) / se
    try:
        from scipy.stats import norm

        p = 2 * (1 - norm.cdf(abs(z)))
    except Exception:
        import math

        p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    p_out = float(p)
    if p_out < 0.0001:
        p_display = 0.0001
    else:
        p_display = round(p_out, 4)
    return {
        "z_stat": round(float(z), 4),
        "p_value": p_display,
        "rate_a": round(rate_a * 100, 2),
        "rate_b": round(rate_b * 100, 2),
    }


def _summarize_leg_level(
    day_results: list[CopytradeDayResult],
) -> dict[str, float | int | None]:
    legs = [
        lg
        for d in day_results
        if d.status == "complete"
        for lg in d.legs
        if lg.status == "complete"
    ]
    if not legs:
        return {
            "leg_n_complete": 0,
            "leg_win_rate_gross_pct": None,
            "leg_win_rate_return_pct": None,
        }
    n = len(legs)
    return {
        "leg_n_complete": n,
        "leg_win_rate_gross_pct": round(
            sum(1 for lg in legs if lg.pnl_ntd > 0) / n * 100.0, 2
        ),
        "leg_win_rate_return_pct": round(
            sum(1 for lg in legs if lg.return_pct > 0) / n * 100.0, 2
        ),
    }




def _complete_signal_days(signal_days: list[dict]) -> list[dict]:
    return sorted(
        [
            d
            for d in signal_days
            if d.get("status") == "complete" and d.get("entry_date") and d.get("exit_date")
        ],
        key=lambda d: str(d["signal_date"]),
    )


def simulate_capital_recycling(
    conn: sqlite3.Connection,
    signal_days: list[dict],
    *,
    capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
) -> dict[str, float | int | None]:
    """單池輪動：上一筆 exit 前不接新訊號。"""
    _ = capital_ntd  # 每訊號部署固定；回收 α 直接加總
    days = _complete_signal_days(signal_days)
    n_signals = len(days)
    if not days:
        return {
            "n_signals": n_signals,
            "recycled_n_cycles": 0,
            "recycled_total_alpha_ntd": 0.0,
            "recycled_total_pnl_ntd": 0.0,
            "recycled_locked_days": 0,
            "alpha_per_locked_day": None,
            "alpha_per_cycle": None,
            "signal_capture_pct": None,
        }
    freed: str | None = None
    cycles = 0
    total_alpha = 0.0
    total_pnl = 0.0
    locked_days = 0
    for d in days:
        entry = str(d["entry_date"])
        exit_d = str(d["exit_date"])
        if freed is not None and entry <= freed:
            continue
        total_alpha += float(d.get("alpha_ntd") or 0)
        total_pnl += float(d.get("pnl_ntd") or 0)
        locked_days += count_hold_trading_days(conn, entry, exit_d)
        cycles += 1
        freed = exit_d
    capture = round(100.0 * cycles / n_signals, 2) if n_signals else None
    return {
        "n_signals": n_signals,
        "recycled_n_cycles": cycles,
        "recycled_total_alpha_ntd": round(total_alpha, 2),
        "recycled_total_pnl_ntd": round(total_pnl, 2),
        "recycled_locked_days": locked_days,
        "alpha_per_locked_day": (
            round(total_alpha / locked_days, 4) if locked_days else None
        ),
        "alpha_per_cycle": round(total_alpha / cycles, 2) if cycles else None,
        "signal_capture_pct": capture,
    }


def select_executed_signal_days(
    signal_days: list[dict],
    *,
    n_slots: int = 1,
) -> tuple[list[dict], dict[str, int | float | None]]:
    """槽位模擬：exit 日收盤釋放，同日不可接新 entry（active if exit_date >= entry_date）。"""
    days = _complete_signal_days(signal_days)
    n_signals = len(days)
    if not days or n_slots < 1:
        return days, {
            "n_signals": n_signals,
            "recycled_n_cycles": len(days),
            "signal_capture_pct": 100.0 if days else None,
            "peak_concurrent_slots": 1 if days else 0,
            "n_skipped": 0,
        }
    slot_exits: list[str] = []
    peak = 0
    executed: list[dict] = []
    skipped = 0
    for d in days:
        entry = str(d["entry_date"])
        exit_d = str(d["exit_date"])
        slot_exits = [e for e in slot_exits if e >= entry]
        if len(slot_exits) >= n_slots:
            skipped += 1
            continue
        slot_exits.append(exit_d)
        peak = max(peak, len(slot_exits))
        executed.append(d)
    cycles = len(executed)
    capture = round(100.0 * cycles / n_signals, 2) if n_signals else None
    return executed, {
        "n_signals": n_signals,
        "recycled_n_cycles": cycles,
        "signal_capture_pct": capture,
        "peak_concurrent_slots": peak,
        "n_skipped": skipped,
    }


def simulate_fixed_slots(
    conn: sqlite3.Connection,
    signal_days: list[dict],
    *,
    n_slots: int = 1,
    capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
) -> dict[str, float | int | None]:
    """固定槽位：exit 日釋放；同日不可接新 entry。"""
    _ = capital_ntd
    days = _complete_signal_days(signal_days)
    n_signals = len(days)
    if not days or n_slots < 1:
        return simulate_capital_recycling(conn, signal_days, capital_ntd=capital_ntd)
    executed, slot_meta = select_executed_signal_days(days, n_slots=n_slots)
    cycles = int(slot_meta["recycled_n_cycles"] or 0)
    total_alpha = sum(float(d.get("alpha_ntd") or 0) for d in executed)
    total_pnl = sum(float(d.get("pnl_ntd") or 0) for d in executed)
    locked_days = sum(
        count_hold_trading_days(conn, str(d["entry_date"]), str(d["exit_date"]))
        for d in executed
    )
    capture = slot_meta["signal_capture_pct"]
    out = simulate_capital_recycling(conn, signal_days, capital_ntd=capital_ntd)
    out.update(
        {
            "recycled_n_cycles": cycles,
            "recycled_total_alpha_ntd": round(total_alpha, 2),
            "recycled_total_pnl_ntd": round(total_pnl, 2),
            "recycled_locked_days": locked_days,
            "alpha_per_locked_day": (
                round(total_alpha / locked_days, 4) if locked_days else None
            ),
            "alpha_per_cycle": round(total_alpha / cycles, 2) if cycles else None,
            "signal_capture_pct": capture,
            "peak_concurrent_slots": slot_meta.get("peak_concurrent_slots"),
        }
    )
    return out


def build_fixed_slots_cycle_rows(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    etf_code: str,
    per_signal_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
    n_slots: int | None = None,
    slots_mode: str = "fixed",
    total_capital_ntd: float | None = None,
    entry_rows: tuple[str, ...] = ("L1",),
    alpha: float = 0.05,
) -> list[dict]:
    from stock_db import load_copytrade_horizon_decay, load_copytrade_signal_days_for_run

    decay = load_copytrade_horizon_decay(conn, batch_id)
    by_row: dict[str, list[dict]] = {}
    for row in decay:
        er = str(row["entry_row"])
        by_row.setdefault(er, []).append(dict(row))

    out: list[dict] = []
    for entry_row in entry_rows:
        prev_recycled = 0.0
        for r in sorted(by_row.get(entry_row, []), key=lambda x: int(x["horizon"])):
            h = int(r["horizon"])
            run_id = str(r["run_id"])
            signal_days = [dict(d) for d in load_copytrade_signal_days_for_run(conn, run_id)]
            if slots_mode == "rotation":
                cap = float(total_capital_ntd or per_signal_ntd * h)
                slots = h
                per = cap / slots if slots else per_signal_ntd
            elif slots_mode == "match_horizon":
                cap = per_signal_ntd * h
                slots = h
                per = per_signal_ntd
            else:
                slots = n_slots or max(1, int((total_capital_ntd or 90_000) / per_signal_ntd))
                cap = slots * per_signal_ntd
                per = per_signal_ntd
            sim = simulate_fixed_slots(conn, signal_days, n_slots=slots, capital_ntd=per)
            uncon_alpha = float(r["total_alpha_ntd"] or 0)
            p_w = r.get("p_value_wilcoxon")
            p_f = float(p_w) if p_w is not None else None
            recycled = float(sim["recycled_total_alpha_ntd"] or 0)
            out.append(
                {
                    "etf_code": etf_code,
                    "entry_row": entry_row,
                    "horizon": h,
                    "capital_ntd": cap,
                    "n_slots": slots,
                    "per_signal_ntd": per,
                    "slots_mode": slots_mode,
                    "strategy_id": str(r["strategy_id"]),
                    "run_id": run_id,
                    "n_signals": int(sim["n_signals"] or 0),
                    "unconstrained_total_alpha_ntd": uncon_alpha,
                    "p_value_wilcoxon": p_f,
                    "is_significant": int(p_f is not None and p_f < alpha),
                    "recycled_n_cycles": int(sim["recycled_n_cycles"] or 0),
                    "recycled_total_alpha_ntd": recycled,
                    "recycled_total_pnl_ntd": sim["recycled_total_pnl_ntd"],
                    "recycled_locked_days": int(sim["recycled_locked_days"] or 0),
                    "alpha_per_locked_day": sim["alpha_per_locked_day"],
                    "alpha_per_cycle": sim["alpha_per_cycle"],
                    "signal_capture_pct": sim["signal_capture_pct"],
                    "peak_concurrent_slots": sim.get("peak_concurrent_slots"),
                    "marginal_recycled_alpha_ntd": round(recycled - prev_recycled, 2),
                }
            )
            prev_recycled = recycled
    return out


def run_fixed_slots_analysis(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    etf_code: str,
    per_signal_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
    n_slots: int | None = None,
    slots_mode: str = "fixed",
    total_capital_ntd: float | None = None,
    entry_rows: tuple[str, ...] = ("L1",),
    persist: bool = True,
) -> list[dict]:
    from stock_db import persist_copytrade_capital_slots

    rows = build_fixed_slots_cycle_rows(
        conn,
        batch_id=batch_id,
        etf_code=etf_code,
        per_signal_ntd=per_signal_ntd,
        n_slots=n_slots,
        slots_mode=slots_mode,
        total_capital_ntd=total_capital_ntd,
        entry_rows=entry_rows,
    )
    if persist:
        persist_copytrade_capital_slots(conn, batch_id, rows)
    return rows


def format_rotation_capital_markdown(
    rows: list[dict],
    *,
    etf_code: str,
    batch_id: str,
    total_capital_ntd: float,
    entry_row: str = "L1",
) -> str:
    """固定總本金、H 日輪動（每日 deploy = capital/H）的持有天數研究。"""
    from datetime import date as date_cls

    sub = sorted(
        [r for r in rows if r["entry_row"] == entry_row],
        key=lambda r: int(r["horizon"]),
    )
    if not sub:
        return ""

    ins_total = summarize_capital_cycle_insights(sub, entry_row)
    ins_eff = summarize_capital_cycle_insights(sub, entry_row)
    best_yield = max(sub, key=lambda r: float(r.get("alpha_yield_on_capital_pct") or 0))
    sweet_h = int(ins_total["sweet_spot_h"])
    eff_h = int(ins_eff.get("best_efficiency_h") or sweet_h)
    today = date_cls.today().strftime("%Y%m%d")

    lines = [
        f"# {etf_code} 固定本金轮动天数研究（L1 · {total_capital_ntd:,.0f} NTD）",
        "",
        f"> batch `{batch_id}` · 报告日 {today}",
        "",
        "## 模型",
        "",
        f"总本金固定 **{total_capital_ntd:,.0f} NTD**，持有 **H** 个交易日卖出：",
        "",
        "- 槽位数 = **H**（H 日轮动）",
        f"- 每讯号部署 = **{total_capital_ntd:,.0f} / H**",
        "- 槽满则跳过讯号（与实盘资金约束一致）",
        "",
        "## 结论",
        "",
    ]
    sweet_row = next((r for r in sub if int(r["horizon"]) == sweet_h), sub[0])
    eff_row = next((r for r in sub if int(r["horizon"]) == eff_h), sub[0])
    lines.extend(
        [
            f"- **总回收 α 最大**：**H{sweet_h}** · 回收 "
            f"**{sweet_row['recycled_total_alpha_ntd']:+,.0f} NTD** · "
            f"每日 {sweet_row['per_signal_ntd']:,.0f} · "
            f"捕获 {sweet_row['signal_capture_pct']:.1f}%",
            f"- **α/锁仓日最高**：**H{eff_h}** · "
            f"{eff_row['alpha_per_locked_day']:.2f} NTD/日",
            f"- **本金收益率最高**：**H{int(best_yield['horizon'])}** · "
            f"{best_yield['alpha_yield_on_capital_pct']:.2f}% "
            f"（回收 α / {total_capital_ntd:,.0f}）",
            "",
            "| H | 每日部署 | 回收 α | 轮数 | 捕获% | α/锁仓日 | 本金收益率% |",
            "|---|---------|--------|------|-------|---------|------------|",
        ]
    )
    for r in sub:
        h = int(r["horizon"])
        mark = " **" if h == sweet_h else ""
        end = "**" if mark else ""
        lines.append(
            f"| {mark}H{h}{end} | {r['per_signal_ntd']:,.0f} | "
            f"{r['recycled_total_alpha_ntd']:+,.0f} | "
            f"{r['recycled_n_cycles']} | "
            f"{r['signal_capture_pct']:.1f}% | "
            f"{r['alpha_per_locked_day'] or 0:.2f} | "
            f"{r.get('alpha_yield_on_capital_pct') or 0:.2f} |"
        )
    lines.append("")
    h9 = next((r for r in sub if int(r["horizon"]) == 9), None)
    h10 = next((r for r in sub if int(r["horizon"]) == 10), None)
    if h9 and sweet_h != 9:
        lines.append("### 解读")
        lines.append("")
        lines.append(
            f"- **H9（九日轮动 · 每日 {h9['per_signal_ntd']:,.0f}）**："
            f"回收 {h9['recycled_total_alpha_ntd']:+,.0f}、"
            f"捕获 {h9['signal_capture_pct']:.1f}%。"
        )
        if h10:
            lines.append(
                f"- **H10（十日轮动 · 每日 {h10['per_signal_ntd']:,.0f}）**："
                f"回收 {h10['recycled_total_alpha_ntd']:+,.0f}、"
                f"捕获 {h10['signal_capture_pct']:.1f}%。"
            )
        lines.append(
            f"- 本样本在 **H{sweet_h}** 达总回收峰值；"
            "若优先每天跟满讯号，选捕获率 100% 的最短 H；"
            "若优先绝对 α 可接受漏单。"
        )
        lines.append("")
    return "\n".join(lines)


def format_fixed_capital_horizon_markdown(
    *,
    etf_code: str,
    batch_id: str,
    per_signal_ntd: float,
    single_pool_rows: list[dict],
    fixed_slot_rows: list[dict],
    match_horizon_rows: list[dict] | None = None,
    n_slots: int | None = None,
) -> str:
    """三種資金模型下的 Optimal hold (H*) 對照報告。"""
    from datetime import date as date_cls

    slot_n = n_slots or int(90_000 / per_signal_ntd)
    capital_ntd = slot_n * per_signal_ntd
    today = date_cls.today().strftime("%Y%m%d")

    lines = [
        f"# {etf_code} 固定本金持有天數研究（L1 · T+1 進場）",
        "",
        f"> batch `{batch_id}` · 每訊號 {per_signal_ntd:,.0f} NTD · "
        f"報告日 {today}",
        "",
        "## 研究設計",
        "",
        "| 模型 | 本金 | 行為 | 選 H 指標 |",
        "|------|------|------|-----------|",
        "| **A 单池** | 1 万 | 一笔轮动，重叠跳过 | `recycled_total_alpha_ntd` |",
        f"| **B 固定槽** | {capital_ntd:,.0f}（{slot_n} 槽） | "
        f"最多 {slot_n} 笔同时持仓 | `recycled_total_alpha_ntd` |",
        "| **C 槽=H** | H × 1 万 | 每个 H 用 H 槽（全捕获对照） | `recycled_total_alpha_ntd` |",
        "| **D 无约束** | 隐含 H×1 万 | 每日讯号都买（不模拟跳过） | `total_alpha_ntd` |",
        "",
        "进场固定 **L1**（T+1 开盘买），出场为持有 H 个交易日收盘卖。",
        "",
    ]

    def _section(
        title: str,
        rows: list[dict],
        note: str,
    ) -> None:
        pool = [r for r in rows if r["entry_row"] == "L1"]
        if not pool:
            return
        ins = summarize_capital_cycle_insights(pool, "L1")
        lines.append(f"## {title}")
        lines.append("")
        lines.append(note)
        lines.append("")
        if ins:
            lines.append(
                f"- **Optimal hold (H*) H{ins['sweet_spot_h']}**：回收 α "
                f"{ins['sweet_spot_recycled_alpha_ntd']:+,.0f} NTD · "
                f"{ins['sweet_spot_n_cycles']} 轮 · "
                f"锁仓日均 {ins['sweet_spot_alpha_per_locked_day']:.1f} NTD"
            )
            eff_h = ins.get("best_efficiency_h")
            if eff_h and eff_h != ins["sweet_spot_h"]:
                eff_row = next(
                    (r for r in pool if int(r["horizon"]) == int(eff_h)),
                    None,
                )
                if eff_row:
                    lines.append(
                        f"- **效率峰值 H{eff_h}**：α/锁仓日 "
                        f"{eff_row['alpha_per_locked_day']:.1f} NTD"
                    )
            lines.append(
                f"- 建议持有至 **H{ins['hold_through_h']}**（边际回收 α 递减）"
            )
        lines.append("")
        lines.append("| H | 无约束α | 回收α | 轮数 | 捕获% | α/锁仓日 | 峰值槽 | Δ回收α |")
        lines.append("|---|--------|-------|------|-------|---------|--------|--------|")
        for r in sorted(pool, key=lambda x: int(x["horizon"])):
            sweet_h = int(ins["sweet_spot_h"]) if ins else -1
            mark = " **" if int(r["horizon"]) == sweet_h else ""
            mark_end = "**" if mark else ""
            peak = r.get("peak_concurrent_slots", "—")
            lines.append(
                f"| {mark}H{r['horizon']}{mark_end} | "
                f"{r.get('unconstrained_total_alpha_ntd', 0):+,.0f} | "
                f"{r['recycled_total_alpha_ntd']:+,.0f} | "
                f"{r['recycled_n_cycles']} | "
                f"{r['signal_capture_pct']:.1f}% | "
                f"{r['alpha_per_locked_day'] or 0:.1f} | "
                f"{peak} | "
                f"{r['marginal_recycled_alpha_ntd']:+,.0f} |"
            )
        lines.append("")

    single_l1 = [r for r in single_pool_rows if r["entry_row"] == "L1"]
    _section(
        "模型 A · 单池 1 万",
        single_l1,
        "> 同一笔钱轮流用；持仓期间新讯号全部跳过。",
    )
    _section(
        f"模型 B · 固定 {slot_n} 槽（{capital_ntd:,.0f} NTD）",
        fixed_slot_rows,
        f"> 最多 {slot_n} 笔同时持仓，每讯号 {per_signal_ntd:,.0f} NTD；"
        "槽位释放规则同单池（exit 日后才可接新 entry）。",
    )
    if match_horizon_rows:
        _section(
            "模型 C · 槽位数 = H（全捕获对照）",
            match_horizon_rows,
            "> 每个 H 允许 H 个同时持仓（本金 = H×1 万）；"
            "检验讯号品质曲线，不受漏单惩罚。",
        )

    # Cross-model summary for L1
    lines.append("## 结论摘要（L1）")
    lines.append("")
    ins_single = (
        summarize_capital_cycle_insights(single_l1, "L1") if single_l1 else None
    )
    lines.append("| 模型 | H* (optimal hold) | 回收 α | 轮数 | α/锁仓日 | 峰值槽 |")
    lines.append("|------|--------|--------|------|---------|--------|")
    for label, ins, rows in (
        (
            "单池 1 万",
            summarize_capital_cycle_insights(single_l1, "L1") if single_l1 else None,
            single_l1,
        ),
        (
            f"固定 {slot_n} 槽",
            summarize_capital_cycle_insights(
                [r for r in fixed_slot_rows if r["entry_row"] == "L1"],
                "L1",
            )
            if fixed_slot_rows
            else None,
            [r for r in fixed_slot_rows if r["entry_row"] == "L1"],
        ),
        (
            "槽 = H",
            summarize_capital_cycle_insights(
                [r for r in match_horizon_rows if r["entry_row"] == "L1"],
                "L1",
            )
            if match_horizon_rows
            else None,
            match_horizon_rows or [],
        ),
    ):
        if not ins:
            continue
        sweet_h = int(ins["sweet_spot_h"])
        sweet_row = next(
            (r for r in rows if int(r["horizon"]) == sweet_h),
            None,
        )
        peak = (
            int(sweet_row["peak_concurrent_slots"])
            if sweet_row and sweet_row.get("peak_concurrent_slots") is not None
            else "—"
        )
        lines.append(
            f"| {label} | H{ins['sweet_spot_h']} | "
            f"{ins['sweet_spot_recycled_alpha_ntd']:+,.0f} | "
            f"{ins['sweet_spot_n_cycles']} | "
            f"{ins['sweet_spot_alpha_per_locked_day']:.1f} | "
            f"{peak} |"
        )
    lines.append("")

    fixed_l1 = [r for r in fixed_slot_rows if r["entry_row"] == "L1"]
    if fixed_l1:
        ins_eff = summarize_capital_cycle_insights(fixed_l1, "L1")
        eff_h = int(ins_eff.get("best_efficiency_h") or 0)
        eff_row = next((r for r in fixed_l1 if int(r["horizon"]) == eff_h), None)
        sweet_h = int(ins_eff["sweet_spot_h"])
        max_peak = max(int(r.get("peak_concurrent_slots") or 0) for r in fixed_l1)
        peak_h9_row = next((r for r in fixed_l1 if int(r["horizon"]) == 9), None)
        peak_h9 = (
            int(peak_h9_row["peak_concurrent_slots"])
            if peak_h9_row and peak_h9_row.get("peak_concurrent_slots") is not None
            else None
        )
        lines.append("### 解读")
        lines.append("")
        h9_row = next((r for r in fixed_l1 if int(r["horizon"]) == 9), None)
        if h9_row and sweet_h != 9:
            lines.append(
                f"- **9 万 × H9（全捕获）**：回收 α **+{h9_row['recycled_total_alpha_ntd']:,.0f}**、"
                f"捕获 {h9_row['signal_capture_pct']:.1f}%、"
                f"峰值 {h9_row['peak_concurrent_slots']} 槽。"
                f"延长到 H{sweet_h} 总回收升至 "
                f"**+{ins_eff['sweet_spot_recycled_alpha_ntd']:,.0f}**，"
                f"但捕获降至 "
                f"{next(r for r in fixed_l1 if int(r['horizon'])==sweet_h)['signal_capture_pct']:.1f}%。"
                "这是 **绝对 α vs 讯号覆盖率** 的权衡，不是单池 H9 矛盾。"
            )
        elif peak_h9 is not None and peak_h9 < slot_n:
            lines.append(
                f"- **9 万本金在 H9 未绑紧**：持 9 日时峰值并发仅 **{peak_h9}/{slot_n}** 槽。"
            )
        elif max_peak >= slot_n:
            lines.append(
                f"- **本金在短 H 会绑紧**：峰值并发 **{max_peak}/{slot_n}** 槽；"
                f"H≥9 时 9 槽可覆盖本样本大部分讯号。"
            )
        if ins_eff and eff_h and eff_h != sweet_h and eff_row:
            lines.append(
                f"- **效率峰值（α/锁仓日）**：H{eff_h} = "
                f"{eff_row['alpha_per_locked_day']:.1f} NTD/日，"
                f"高于总回收 Optimal hold H{sweet_h}（{ins_eff['sweet_spot_alpha_per_locked_day']:.1f}）。"
                f"但 H{eff_h} 总回收仅 {eff_row['recycled_total_alpha_ntd']:+,.0f} NTD，"
                "短持高效益来自样本极少轮次，不宜单独作为持有期决策。"
            )
        lines.append(
            f"- **单池 1 万**仍应以 H{ins_single['sweet_spot_h']} 为 Optimal hold (H*)"
            f"（回收 {ins_single['sweet_spot_recycled_alpha_ntd']:+,.0f}），"
            "因同一笔钱无法叠仓。"
            if ins_single
            else ""
        )
        lines.append("")
    return "\n".join(lines)


def build_capital_cycle_rows(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    etf_code: str,
    entry_rows: tuple[str, ...] = ("L1", "L2", "L3"),
    capital_ntd: float = 10_000.0,
    alpha: float = 0.05,
) -> list[dict]:
    from stock_db import load_copytrade_horizon_decay, load_copytrade_signal_days_for_run

    decay = load_copytrade_horizon_decay(conn, batch_id)
    by_row: dict[str, list[dict]] = {}
    for row in decay:
        er = str(row["entry_row"])
        by_row.setdefault(er, []).append(dict(row))

    out: list[dict] = []
    for entry_row in entry_rows:
        prev_uncon = 0.0
        prev_recycled = 0.0
        for r in sorted(by_row.get(entry_row, []), key=lambda x: int(x["horizon"])):
            h = int(r["horizon"])
            run_id = str(r["run_id"])
            signal_days = load_copytrade_signal_days_for_run(conn, run_id)
            sim = simulate_capital_recycling(conn, [dict(d) for d in signal_days])
            uncon_alpha = float(r["total_alpha_ntd"] or 0)
            p_w = r.get("p_value_wilcoxon")
            p_f = float(p_w) if p_w is not None else None
            recycled = float(sim["recycled_total_alpha_ntd"] or 0)
            out.append(
                {
                    "etf_code": etf_code,
                    "entry_row": entry_row,
                    "horizon": h,
                    "capital_ntd": capital_ntd,
                    "strategy_id": str(r["strategy_id"]),
                    "run_id": run_id,
                    "n_signals": int(sim["n_signals"] or 0),
                    "unconstrained_total_alpha_ntd": uncon_alpha,
                    "unconstrained_alpha_per_day": (
                        round(uncon_alpha / h, 4) if h else None
                    ),
                    "marginal_unconstrained_alpha_ntd": round(
                        uncon_alpha - prev_uncon, 2
                    ),
                    "p_value_wilcoxon": p_f,
                    "is_significant": int(p_f is not None and p_f < alpha),
                    "recycled_n_cycles": int(sim["recycled_n_cycles"] or 0),
                    "recycled_total_alpha_ntd": recycled,
                    "recycled_total_pnl_ntd": sim["recycled_total_pnl_ntd"],
                    "recycled_locked_days": int(sim["recycled_locked_days"] or 0),
                    "alpha_per_locked_day": sim["alpha_per_locked_day"],
                    "alpha_per_cycle": sim["alpha_per_cycle"],
                    "signal_capture_pct": sim["signal_capture_pct"],
                    "marginal_recycled_alpha_ntd": round(recycled - prev_recycled, 2),
                }
            )
            prev_uncon = uncon_alpha
            prev_recycled = recycled
    return out


def summarize_capital_cycle_insights(
    cycle_rows: list[dict],
    entry_row: str = "L1",
) -> dict[str, object]:
    """有限資金池下的 Optimal hold (H*) 與邊際遞減點。"""
    sub = sorted(
        [r for r in cycle_rows if r["entry_row"] == entry_row],
        key=lambda r: int(r["horizon"]),
    )
    if not sub:
        return {}

    best_total = max(sub, key=lambda r: float(r["recycled_total_alpha_ntd"] or 0))
    best_eff = max(sub, key=lambda r: float(r["alpha_per_locked_day"] or 0))
    sweet_h = int(best_total["horizon"])

    # After H*: marginal recycled α turns negative or < 25% of H* marginal
    sweet_marg = float(best_total.get("marginal_recycled_alpha_ntd") or 0)
    threshold = max(sweet_marg * 0.25, 500.0)
    plateau_end: int | None = None
    for r in sub:
        h = int(r["horizon"])
        if h <= sweet_h:
            continue
        marg = float(r.get("marginal_recycled_alpha_ntd") or 0)
        if marg < threshold:
            plateau_end = h - 1
            break
    if plateau_end is None:
        plateau_end = int(sub[-1]["horizon"])

    first_sig = next(
        (int(r["horizon"]) for r in sub if int(r.get("is_significant") or 0)),
        None,
    )

    return {
        "entry_row": entry_row,
        "sweet_spot_h": sweet_h,
        "sweet_spot_recycled_alpha_ntd": best_total["recycled_total_alpha_ntd"],
        "sweet_spot_alpha_per_locked_day": best_total["alpha_per_locked_day"],
        "sweet_spot_n_cycles": best_total["recycled_n_cycles"],
        "best_efficiency_h": int(best_eff["horizon"]),
        "first_significant_h": first_sig,
        "hold_through_h": plateau_end,
        "capital_cycle_trading_days": sweet_h,
    }


def build_copytrade_research_conclusions(
    *,
    batch_id: str,
    etf_code: str,
    capital_ntd: float,
    max_hold: int,
    decay_rows: list[dict],
    cycle_rows: list[dict],
    entry_rows: tuple[str, ...] = ("L1", "L2", "L3"),
) -> list[dict]:
    """彙整 decay + 資金週期結論，供 persist_copytrade_research_conclusions。"""
    import json

    conclusions: list[dict] = []
    for entry_row in entry_rows:
        decay_ins = summarize_decay_insights(decay_rows, entry_row)
        cycle_ins = summarize_capital_cycle_insights(cycle_rows, entry_row)
        if not decay_ins and not cycle_ins:
            continue

        if decay_ins:
            fs = decay_ins.get("first_significant_h")
            ls = decay_ins.get("last_significant_h")
            if decay_ins.get("all_horizons_insignificant"):
                decay_text = (
                    f"{entry_row}：全 H1–H{max_hold} 相對台指皆無顯著超額（Wilcoxon p>0.05）。"
                )
            elif fs:
                decay_text = (
                    f"{entry_row}：H1–H{fs - 1 if fs > 1 else 0} 與台指無顯著差異；"
                    f"首次顯著勝台指 H{fs}"
                    + (f"，持續至 H{ls}" if ls else "")
                    + f"。"
                    f"無限資金累計 α 峰值 H{decay_ins['peak_h']}（"
                    f"{decay_ins['peak_alpha_ntd']:+,.0f} NTD）。"
                )
            else:
                decay_text = f"{entry_row}：decay 無顯著 H。"
            conclusions.append(
                {
                    "etf_code": etf_code,
                    "analysis_type": "horizon_decay",
                    "entry_row": entry_row,
                    "metric_key": "summary",
                    "horizon": decay_ins.get("peak_h"),
                    "metric_value": decay_ins.get("peak_alpha_ntd"),
                    "conclusion_zh": decay_text,
                    "details_json": json.dumps(decay_ins, ensure_ascii=False),
                }
            )

        if cycle_ins:
            sh = cycle_ins["sweet_spot_h"]
            cycle_text = (
                f"{entry_row} 有限資金（單池 {capital_ntd:,.0f} NTD）："
                f"資金週期 Optimal hold (H*) **H{sh}**（持有 {sh} 交易日後賣出再輪入下一筆 T+1）。"
                f"回收 α {cycle_ins['sweet_spot_recycled_alpha_ntd']:+,.0f} NTD / "
                f"{cycle_ins['sweet_spot_n_cycles']} 輪，"
                f"鎖倉日均 α {cycle_ins['sweet_spot_alpha_per_locked_day']:.1f} NTD。"
                f"延長至 H>{cycle_ins['hold_through_h']} 邊際回收 α 明顯遞減。"
            )
            if cycle_ins.get("first_significant_h"):
                cycle_text += (
                    f" 統計上首次顯著超額為 H{cycle_ins['first_significant_h']}，"
                    f"但有限資金下總回收以 H{sh} 最佳。"
                )
            conclusions.append(
                {
                    "etf_code": etf_code,
                    "analysis_type": "capital_cycle",
                    "entry_row": entry_row,
                    "metric_key": "sweet_spot",
                    "horizon": sh,
                    "metric_value": cycle_ins["sweet_spot_recycled_alpha_ntd"],
                    "conclusion_zh": cycle_text,
                    "details_json": json.dumps(cycle_ins, ensure_ascii=False),
                }
            )

    # 跨分析 L1 執行建議
    l1_cycle = summarize_capital_cycle_insights(cycle_rows, "L1")
    l1_decay = summarize_decay_insights(decay_rows, "L1")
    if l1_cycle and l1_decay:
        sh = int(l1_cycle["sweet_spot_h"])
        conclusions.append(
            {
                "etf_code": etf_code,
                "analysis_type": "capital_cycle",
                "entry_row": "L1",
                "metric_key": "actionable",
                "horizon": sh,
                "metric_value": l1_cycle["sweet_spot_recycled_alpha_ntd"],
                "conclusion_zh": (
                    f"【執行建議·L1】訊號日 T 收盤後偵測 → T+1 開盤買入 → "
                    f"持有 {sh} 個交易日收盤賣出 → 資金釋放後接下一可執行訊號。"
                    f"短於 H{sh} 總回收 α 偏低；"
                    f"長於 H{l1_cycle['hold_through_h']} 邊際效益遞減。"
                    f"（回測 batch `{batch_id}`，每日標的 {capital_ntd:,.0f} NTD）"
                ),
                "details_json": json.dumps(
                    {"decay": l1_decay, "cycle": l1_cycle},
                    ensure_ascii=False,
                ),
            }
        )
    return conclusions


def run_capital_cycle_analysis(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    etf_code: str,
    capital_ntd: float = 10_000.0,
    max_hold: int = 20,
    entry_rows: tuple[str, ...] = ("L1", "L2", "L3"),
    persist: bool = True,
) -> tuple[list[dict], list[dict]]:
    from stock_db import (
        load_copytrade_horizon_decay,
        persist_copytrade_capital_cycle,
        persist_copytrade_research_conclusions,
    )

    cycle_rows = build_capital_cycle_rows(
        conn,
        batch_id=batch_id,
        etf_code=etf_code,
        entry_rows=entry_rows,
        capital_ntd=capital_ntd,
    )
    decay_rows = [dict(r) for r in load_copytrade_horizon_decay(conn, batch_id)]
    conclusions = build_copytrade_research_conclusions(
        batch_id=batch_id,
        etf_code=etf_code,
        capital_ntd=capital_ntd,
        max_hold=max_hold,
        decay_rows=decay_rows,
        cycle_rows=cycle_rows,
        entry_rows=entry_rows,
    )
    if persist:
        persist_copytrade_capital_cycle(conn, batch_id, cycle_rows)
        persist_copytrade_research_conclusions(
            conn,
            batch_id,
            conclusions,
            replace_types=("horizon_decay", "capital_cycle"),
        )
    return cycle_rows, conclusions


def format_capital_cycle_markdown(
    cycle_rows: list[dict],
    conclusions: list[dict],
    *,
    etf_code: str,
    capital_ntd: float,
    batch_id: str,
) -> str:
    lines = [
        "## 有限資金週轉（單池輪動）",
        "",
        f"> 假設總資金僅 **{capital_ntd:,.0f} NTD** 一池；"
        "出場後才接下一筆可執行訊號（略過持倉期重疊訊號）。",
        "Optimal hold (H*) = 最大化 **回收累計 α**（相對台指）的持有天數 H。",
        "",
    ]
    for entry_row in ("L1", "L2", "L3"):
        ins = summarize_capital_cycle_insights(cycle_rows, entry_row)
        sub = [r for r in cycle_rows if r["entry_row"] == entry_row]
        if not sub or not ins:
            continue
        lines.append(f"### {entry_row}")
        lines.append(
            f"- **Optimal hold (H*) H{ins['sweet_spot_h']}**：回收 α "
            f"{ins['sweet_spot_recycled_alpha_ntd']:+,.0f} NTD · "
            f"{ins['sweet_spot_n_cycles']} 輪 · "
            f"鎖倉日均 {ins['sweet_spot_alpha_per_locked_day']:.1f} NTD"
        )
        lines.append(
            f"- 建議持有至 **H{ins['hold_through_h']}**；"
            f"再延長邊際回收 α 遞減"
        )
        lines.append("")
        lines.append(
            "| H | 無約束α | 回收α | 輪數 | 捕獲% | α/鎖倉日 | Δ回收α |"
        )
        lines.append("|---|--------|-------|------|-------|---------|--------|")
        for r in sorted(sub, key=lambda x: int(x["horizon"])):
            mark = (
                " **"
                if int(r["horizon"]) == int(ins["sweet_spot_h"])
                else ""
            )
            mark_end = "**" if mark else ""
            lines.append(
                f"| {mark}H{r['horizon']}{mark_end} | "
                f"{r['unconstrained_total_alpha_ntd']:+,.0f} | "
                f"{r['recycled_total_alpha_ntd']:+,.0f} | "
                f"{r['recycled_n_cycles']} | "
                f"{r['signal_capture_pct']:.1f}% | "
                f"{r['alpha_per_locked_day'] or 0:.1f} | "
                f"{r['marginal_recycled_alpha_ntd']:+,.0f} |"
            )
        lines.append("")

    actionable = [
        c
        for c in conclusions
        if c.get("metric_key") == "actionable" and c.get("entry_row") == "L1"
    ]
    if actionable:
        lines.append("### 執行建議")
        lines.append("")
        lines.append(actionable[0]["conclusion_zh"])
        lines.append("")

    return "\n".join(lines)


def run_strategies(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    capital_ntd: float = 10_000.0,
    cost_bps: float = 0.0,
    strategies: tuple[dict, ...],
    window_start: str | None = None,
    window_end: str | None = None,
    persist: bool = True,
    run_suffix: str | None = None,
    batch_id: str | None = None,
) -> list[CopytradeRunResult]:
    from stock_db import persist_copytrade_horizon_decay

    signals = iter_copytrade_signals(
        conn, etf_code, window_start=window_start, window_end=window_end
    )
    grouped = group_signals_by_date(signals)
    beta_map, _ = load_stock_beta_map(conn)

    results: list[CopytradeRunResult] = []
    for spec in strategies:
        result = run_copytrade_backtest(
            conn,
            etf_code,
            strategy_id=spec["strategy_id"],
            strategy_label=spec["strategy_label"],
            entry_lag_days=int(spec["entry_lag_days"]),
            hold_trading_days=int(spec["hold_trading_days"]),
            entry_price_mode=str(spec.get("entry_price_mode", "open")),
            capital_ntd=capital_ntd,
            cost_bps=cost_bps,
            window_start=window_start,
            window_end=window_end,
            run_suffix=run_suffix,
            batch_id=batch_id,
            grouped=grouped,
            beta_map=beta_map,
        )
        if persist:
            persist_copytrade_run(conn, result)
        results.append(result)

    if persist and batch_id and results:
        decay_rows = build_horizon_decay_rows(results, etf_code)
        persist_copytrade_horizon_decay(conn, batch_id, decay_rows)
        max_hold = max(r.hold_trading_days for r in results)
        run_capital_cycle_analysis(
            conn,
            batch_id=batch_id,
            etf_code=etf_code,
            capital_ntd=capital_ntd,
            max_hold=max_hold,
            persist=True,
        )

    return results


def run_default_strategies(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    capital_ntd: float = 10_000.0,
    cost_bps: float = 0.0,
    strategies: tuple[dict, ...] | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    persist: bool = True,
    run_suffix: str | None = None,
) -> list[CopytradeRunResult]:
    return run_strategies(
        conn,
        etf_code,
        capital_ntd=capital_ntd,
        cost_bps=cost_bps,
        strategies=strategies or DEFAULT_STRATEGIES,
        window_start=window_start,
        window_end=window_end,
        persist=persist,
        run_suffix=run_suffix,
    )


def _matrix_row_key(strategy_id: str) -> str | None:
    if strategy_id.startswith("L0O-"):
        return "L0O"
    if strategy_id.startswith("L0C-"):
        return "L0C"
    for lag in ("L1", "L2", "L3"):
        if strategy_id.startswith(lag):
            return lag
    return None


def _matrix_col_key(strategy_id: str) -> int | None:
    if "-H" in strategy_id:
        part = strategy_id.split("-H", 1)[1]
    elif strategy_id[2:3] == "H":
        part = strategy_id[3:]
    else:
        return None
    try:
        return int(part)
    except ValueError:
        return None


def _cell_pnl_text(result: CopytradeRunResult | None) -> str:
    if result is None or not result.n_complete_days:
        return "—"
    return f"{result.total_pnl_ntd:+,.0f}"


def _cell_ret_text(result: CopytradeRunResult | None) -> str:
    if result is None or result.avg_day_return_pct is None:
        return "—"
    return f"{result.avg_day_return_pct:+.2f}%"


def _cell_alpha_text(result: CopytradeRunResult | None) -> str:
    if result is None or not result.n_complete_days:
        return "—"
    return f"{result.total_alpha_ntd:+,.0f}"


def _sig_mark(p: float | None, alpha: float = 0.05) -> str:
    if p is None:
        return ""
    return "*" if p < alpha else ""


def _matrix_table(
    results: list[CopytradeRunResult],
    *,
    rows: list[str],
    max_hold: int,
    value_fn,
    header: str,
) -> list[str]:
    by_id = {r.strategy_id: r for r in results}
    cols = list(range(1, max_hold + 1))
    header_cells = " | ".join(f"H{h}" for h in cols)
    sep = "|".join(["---"] * len(cols))
    lines = [
        f"### {header}",
        "",
        f"| 進場＼持有 | {header_cells} |",
        f"|------------|{sep}|",
    ]
    for row in rows:
        cells = []
        for h in cols:
            if row == "L0O":
                sid = f"L0O-H{h}"
            elif row == "L0C":
                sid = f"L0C-H{h}"
            else:
                sid = f"{row}H{h}"
            cells.append(value_fn(by_id.get(sid)))
        lines.append(f"| **{row}** | " + " | ".join(cells) + " |")
    return lines


def format_copytrade_matrix_markdown(
    results: list[CopytradeRunResult],
    *,
    etf_code: str,
    capital_ntd: float,
    cost_bps: float,
    max_hold: int = 5,
    batch_id: str | None = None,
    cycle_rows: list[dict] | None = None,
    conclusions: list[dict] | None = None,
) -> str:
    decay_rows = build_horizon_decay_rows(results, etf_code)
    exec_rows = ["L1", "L2", "L3"]
    all_rows = ["L0O", "L0C", "L1", "L2", "L3"]

    lines = [
        f"# {etf_code} 跟單回測矩陣（L×H · H1–H{max_hold}）",
        "",
        f"> {COPYTRADE_VERSION} · 每日 {capital_ntd:,.0f} NTD · 成本 {cost_bps:.0f} bps · "
        f"基準 {TW_SPOT_CODE} · batch `{batch_id or '—'}`",
        "",
        "訊號日 **T** = 持股公布日；**L1–L3** = T+1～T+3 開盤進場；"
        f"**H1–H{max_hold}** = 持有 1～{max_hold} 交易日（收盤出）。",
        "α = 組合損益 − 同期台指同進出規則；`*` = Wilcoxon p<0.05。",
        "",
    ]
    lines.extend(
        _matrix_table(
            results,
            rows=all_rows,
            max_hold=max_hold,
            value_fn=_cell_pnl_text,
            header="累計 Gross 損益 (NTD)",
        )
    )
    lines.append("")
    lines.extend(
        _matrix_table(
            results,
            rows=all_rows,
            max_hold=max_hold,
            value_fn=_cell_alpha_text,
            header="累計 α vs 台指 (NTD)",
        )
    )

    lines.extend(["", "## 顯著性 Decay（日均超額報酬 vs 台指）", ""])
    for entry_row in exec_rows:
        sub = sorted(
            [r for r in decay_rows if r["entry_row"] == entry_row],
            key=lambda r: int(r["horizon"]),
        )
        if not sub:
            continue
        insight = summarize_decay_insights(decay_rows, entry_row)
        lines.append(f"### {entry_row}（可執行進場）")
        if insight:
            lines.append(
                f"- α 峰值：**H{insight['peak_h']}** "
                f"（累計 α {insight['peak_alpha_ntd']:+,.0f} NTD）"
            )
            if insight.get("all_horizons_insignificant"):
                lines.append("- 全 H1–Hmax 與台指**皆無顯著差異**（p>0.05）")
            elif insight.get("first_significant_h"):
                lines.append(
                    f"- 首次顯著勝台指（Wilcoxon p<0.05）：**H{insight['first_significant_h']}**"
                )
                if insight.get("last_significant_h"):
                    lines.append(
                        f"- 末次仍顯著：**H{insight['last_significant_h']}**"
                    )
            else:
                lines.append("- 全窗口與台指無顯著差異")
        lines.append("")
        lines.append("| H | n | 累計α | 日均超額% | t | p(t) | p(W) |")
        lines.append("|---|-----|-------|-----------|-----|------|------|")
        for r in sub:
            p_w = r.get("p_value_wilcoxon")
            mark = _sig_mark(float(p_w) if p_w is not None else None)
            p_t = r.get("p_value_ttest")
            t_s = r.get("t_stat")
            me = r.get("mean_excess_pct")
            me_s = f"{me:+.3f}" if me is not None else "—"
            lines.append(
                f"| H{r['horizon']}{mark} | {r['n_complete']} | "
                f"{r['total_alpha_ntd']:+,.0f} | {me_s} | "
                f"{t_s if t_s is not None else '—'} | "
                f"{p_t if p_t is not None else '—'} | "
                f"{p_w if p_w is not None else '—'} |"
            )
        lines.append("")

    if cycle_rows and batch_id:
        lines.append(
            format_capital_cycle_markdown(
                cycle_rows,
                conclusions or [],
                etf_code=etf_code,
                capital_ntd=capital_ntd,
                batch_id=batch_id,
            )
        )

    if results:
        w0 = results[0]
        lines.extend(
            [
                f"窗口：{w0.window_start or '—'} ～ {w0.window_end or '—'}",
                "",
                "_多重 H 檢定未校正；解讀以 decay 趨勢為主，非單點 p 值。_",
            ]
        )

    return "\n".join(lines) + "\n"


def format_copytrade_summary_markdown(
    results: list[CopytradeRunResult],
    *,
    etf_code: str,
    capital_ntd: float,
    cost_bps: float,
    max_hold: int = 5,
    batch_id: str | None = None,
) -> str:
    if len(results) >= 15:
        return format_copytrade_matrix_markdown(
            results,
            etf_code=etf_code,
            capital_ntd=capital_ntd,
            cost_bps=cost_bps,
            max_hold=max_hold,
            batch_id=batch_id,
        )
    lines = [
        f"# {etf_code} 跟單回測摘要",
        "",
        f"> {COPYTRADE_VERSION} · 每日配置 {capital_ntd:,.0f} NTD · "
        f"成本 {cost_bps:.0f} bps · 基準 {TW_SPOT_CODE}",
        "",
        "| 策略 | 說明 | 有效日 | 累計損益 | 日均報酬% | 勝率% | MaxDD% |",
        "|------|------|--------|----------|-----------|-------|--------|",
    ]
    for r in results:
        pnl = f"{r.total_pnl_ntd:+,.0f}" if r.n_complete_days else "—"
        avg = f"{r.avg_day_return_pct:+.3f}" if r.avg_day_return_pct is not None else "—"
        wr = f"{r.win_rate_pct:.1f}" if r.win_rate_pct is not None else "—"
        mdd = f"{r.max_drawdown_pct:.2f}" if r.max_drawdown_pct is not None else "—"
        lines.append(
            f"| **{r.strategy_id}** | {r.strategy_label} | {r.n_complete_days} | "
            f"{pnl} | {avg} | {wr} | {mdd} |"
        )
    return "\n".join(lines) + "\n"


def write_copytrade_report(
    results: list[CopytradeRunResult],
    *,
    etf_code: str,
    capital_ntd: float,
    cost_bps: float,
    reports_dir: Path | None = None,
    matrix: bool = False,
    max_hold: int = 5,
    batch_id: str | None = None,
    cycle_rows: list[dict] | None = None,
    conclusions: list[dict] | None = None,
) -> Path:
    root = reports_dir or REPORTS_RESEARCH
    root.mkdir(parents=True, exist_ok=True)
    suffix = "_copytrade_matrix" if matrix or len(results) >= 15 else "_copytrade"
    if max_hold > 5:
        suffix = f"_copytrade_h{max_hold}_alpha"
    out = root / f"{date.today().strftime('%Y%m%d')}_{etf_code.lower()}{suffix}.md"
    body = (
        format_copytrade_matrix_markdown(
            results,
            etf_code=etf_code,
            capital_ntd=capital_ntd,
            cost_bps=cost_bps,
            max_hold=max_hold,
            batch_id=batch_id,
            cycle_rows=cycle_rows,
            conclusions=conclusions,
        )
        if matrix or len(results) >= 15
        else format_copytrade_summary_markdown(
            results,
            etf_code=etf_code,
            capital_ntd=capital_ntd,
            cost_bps=cost_bps,
            max_hold=max_hold,
            batch_id=batch_id,
        )
    )
    out.write_text(body, encoding="utf-8")
    return out
