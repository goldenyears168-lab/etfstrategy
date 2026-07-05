"""TW100 slot-based Top-K portfolio · non-overlapping holds · Research layer."""

from __future__ import annotations

import sqlite3
from typing import Any

import pandas as pd

from research.backtest.slot_portfolio_metrics import simulate_slot_portfolio
from research.backtest.tw100_ml_backtest import load_close_panel
from stock_universe import TW100_UNIVERSE_ID, load_universe_constituents

DEFAULT_TOTAL_CAPITAL = 100_000.0
DEFAULT_HOLD_OFFSET_ENTRY = 1  # signal T → enter T+1
DEFAULT_HOLD_OFFSET_EXIT = 3  # signal T → exit T+3 (2-day hold)
DEFAULT_TW_COST_MODEL: dict[str, float] = {
    "buy_fee_pct": 0.1425,
    "sell_fee_pct": 0.1425,
    "sell_tax_pct": 0.3,
}


def periods_from_pred_panel(
    pred: pd.Series,
    calendar: list[str],
    *,
    top_k: int,
    min_cross_section: int = 30,
    entry_offset: int = DEFAULT_HOLD_OFFSET_ENTRY,
    exit_offset: int = DEFAULT_HOLD_OFFSET_EXIT,
) -> list[dict[str, Any]]:
    """Top-K signals per day · entry/exit from trading calendar (label-aligned)."""
    cal_idx = {d: i for i, d in enumerate(calendar)}
    periods: list[dict[str, Any]] = []
    for dt, grp in pred.groupby(level="datetime"):
        signal_date = pd.Timestamp(dt).strftime("%Y-%m-%d")
        i = cal_idx.get(signal_date)
        if i is None or i + exit_offset >= len(calendar):
            continue
        scored = grp.dropna()
        if len(scored) < max(min_cross_section, top_k):
            continue
        entry_date = calendar[i + entry_offset]
        exit_date = calendar[i + exit_offset]
        top = scored.nlargest(top_k)
        for idx in top.index:
            sid = idx[1] if isinstance(idx, tuple) else idx
            periods.append(
                {
                    "stock_id": str(sid),
                    "signal_date": signal_date,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                }
            )
    return periods


def benchmark_periods_from_signals(
    signal_dates: list[str],
    calendar: list[str],
    benchmark_id: str,
    *,
    entry_offset: int = DEFAULT_HOLD_OFFSET_ENTRY,
    exit_offset: int = DEFAULT_HOLD_OFFSET_EXIT,
) -> list[dict[str, Any]]:
    cal_idx = {d: i for i, d in enumerate(calendar)}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for signal_date in sorted(signal_dates):
        if signal_date in seen:
            continue
        i = cal_idx.get(signal_date)
        if i is None or i + exit_offset >= len(calendar):
            continue
        seen.add(signal_date)
        out.append(
            {
                "stock_id": benchmark_id,
                "signal_date": signal_date,
                "entry_date": calendar[i + entry_offset],
                "exit_date": calendar[i + exit_offset],
            }
        )
    return out


def load_tw100_close_panel(
    conn: sqlite3.Connection,
    *,
    snapshot_date: str,
    start: str,
    end: str,
    extra_stock_ids: list[str] | None = None,
    benchmark_index_code: str = "IX0001",
) -> pd.DataFrame:
    watch = load_universe_constituents(conn, TW100_UNIVERSE_ID, snapshot_date)
    stock_ids = [w["stock_id"] for w in watch]
    for sid in extra_stock_ids or []:
        if sid not in stock_ids:
            stock_ids.append(sid)
    panel = load_close_panel(conn, stock_ids, start=start, end=end)
    if extra_stock_ids:
        for sid in extra_stock_ids:
            if sid not in panel.columns or panel[sid].notna().sum() < 50:
                from market_benchmark import load_benchmark_close

                if sid == "0050" and benchmark_index_code:
                    bench = load_benchmark_close(conn, code=benchmark_index_code)
                    bench = bench[(bench.index >= start) & (bench.index <= end)]
                    if not bench.empty:
                        panel[sid] = bench.reindex(panel.index)
    return panel


def run_slot_topk_simulation(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    calendar: list[str],
    periods: list[dict[str, Any]],
    *,
    top_k: int,
    total_capital: float = DEFAULT_TOTAL_CAPITAL,
    cost_model: dict[str, float] | None = DEFAULT_TW_COST_MODEL,
) -> dict[str, Any]:
    trade_dates = [d for d in calendar if d >= (periods[0]["entry_date"] if periods else calendar[0])]
    if periods:
        last_exit = max(p["exit_date"] for p in periods)
        trade_dates = [d for d in calendar if d <= last_exit]
    metrics = simulate_slot_portfolio(
        conn,
        close,
        trade_dates,
        periods,
        total_capital=total_capital,
        n_slots=top_k,
        cost_model=cost_model,
    )
    metrics["portfolio_mode"] = "slot_topk"
    metrics["top_k"] = top_k
    metrics["n_signal_days"] = len({p["signal_date"] for p in periods})
    metrics["cost_model_enabled"] = cost_model is not None
    return metrics


def compare_slot_vs_benchmark(
    strategy: dict[str, Any],
    benchmark: dict[str, Any],
    *,
    benchmark_id: str,
) -> dict[str, Any]:
    s_ret = strategy.get("total_return_pct")
    b_ret = benchmark.get("total_return_pct")
    s_sharpe = strategy.get("sharpe_ratio")
    b_sharpe = benchmark.get("sharpe_ratio")
    excess_return_pct = (
        round(float(s_ret) - float(b_ret), 4)
        if s_ret is not None and b_ret is not None
        else None
    )
    return {
        "benchmark_id": benchmark_id,
        "strategy_total_return_pct": s_ret,
        "benchmark_total_return_pct": b_ret,
        "excess_total_return_pct": excess_return_pct,
        "strategy_sharpe": s_sharpe,
        "benchmark_sharpe": b_sharpe,
        "strategy_max_drawdown_pct": strategy.get("max_drawdown_pct"),
        "benchmark_max_drawdown_pct": benchmark.get("max_drawdown_pct"),
        "strategy_cagr_pct": strategy.get("cagr_pct"),
        "benchmark_cagr_pct": benchmark.get("cagr_pct"),
    }
