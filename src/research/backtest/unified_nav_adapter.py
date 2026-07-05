"""Convert adopted strategy legs/periods → unified daily NAV (comparison layer)."""

from __future__ import annotations

import csv
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from backtest_standard_config import BacktestStandardConfig, TrackAdapterSpec, load_backtest_standard_config
from market_benchmark import load_benchmark_close
from research.backtest.finmind_tx_foreign_common import PROXY_CODE
from research.backtest.chunge_funnel_backtest import VCP_COIL_CLOSE, VCP_PIVOT_GATE, run_chunge_slot_backtest
from research.backtest.copytrade_backtest import select_executed_signal_days
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.slot_backtest_summary import SlotBacktestConfig
from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods
from stock_db import DEFAULT_DB_PATH, connect, load_copytrade_signal_days_for_run

_COPYTRADE_EARLIEST = "2025-05-28"


def _trade_dates(full_dates: list[str], date_start: str, date_end: str) -> list[str]:
    return [d for d in full_dates if date_start <= d <= date_end]


def _copytrade_periods(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    n_slots: int,
) -> list[dict]:
    run = conn.execute(
        """
        SELECT run_id FROM copytrade_runs
        WHERE etf_code = '00981A' AND strategy_id = 'L1H9'
        ORDER BY synced_at DESC LIMIT 1
        """
    ).fetchone()
    if run is None:
        return []
    raw_days = load_copytrade_signal_days_for_run(conn, str(run["run_id"]))
    signal_days = [dict(d) for d in raw_days]
    complete = [d for d in signal_days if str(d.get("status") or "") == "complete"]
    executed, _ = select_executed_signal_days(complete, n_slots=n_slots)
    periods: list[dict] = []
    for d in executed:
        entry = str(d.get("entry_date") or "")
        exit_d = str(d.get("exit_date") or "")
        if not entry or not exit_d or exit_d < date_start or entry > date_end:
            continue
        sid = str(d.get("stock_id") or "")
        if not sid:
            continue
        row: dict[str, Any] = {
            "stock_id": sid,
            "entry_date": entry,
            "exit_date": exit_d,
        }
        if d.get("entry_px") is not None:
            row["entry_px"] = float(d["entry_px"])
        periods.append(row)
    return periods


def _rrg_hold7_periods(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
) -> list[dict]:
    from research.backtest.rrg_mono_backtest import run_breadth_zone_comparison

    result = run_breadth_zone_comparison(conn, date_start=date_start, date_end=date_end)
    return list(result["pooled_all"]["periods"])


def _swap_accel_periods(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
) -> list[dict]:
    from strategy_performance_yearly import _swap_accel_run

    result = _swap_accel_run(conn, date_start=date_start, date_end=date_end)
    return list(result.get("periods") or [])


def _vcp_periods(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    date_start: str,
    date_end: str,
) -> list[dict]:
    cfg_map = {
        "vcp-pivot-gate": VCP_PIVOT_GATE,
        "vcp-coil-close": VCP_COIL_CLOSE,
    }
    base = dict(cfg_map[strategy_id])
    cfg = SlotBacktestConfig(
        date_start=date_start,
        date_end=date_end,
        **{k: v for k, v in base.items() if k not in ("date_start", "date_end")},
    )
    result = run_chunge_slot_backtest(conn, config=cfg)
    return list(result.get("periods") or [])


def collect_periods(
    conn: sqlite3.Connection,
    strategy_id: str,
    *,
    date_start: str,
    date_end: str,
    adapter: TrackAdapterSpec,
) -> list[dict]:
    source = adapter.source
    if source == "copytrade_legs":
        return _copytrade_periods(
            conn, date_start=date_start, date_end=date_end, n_slots=adapter.n_slots
        )
    if source == "finmind_marketplace":
        from research.backtest.finmind_marketplace_ensemble import load_raw_periods

        return load_raw_periods(
            conn, strategy_id, date_start=date_start, date_end=date_end
        )
    if source != "slot_periods_native":
        raise ValueError(f"unknown adapter source: {source}")

    if strategy_id == "rrg-mono-hold7":
        return _rrg_hold7_periods(conn, date_start=date_start, date_end=date_end)
    if strategy_id == "rrg-mono-swap-accel":
        return _swap_accel_periods(conn, date_start=date_start, date_end=date_end)
    if strategy_id.startswith("vcp-"):
        return _vcp_periods(
            conn, strategy_id=strategy_id, date_start=date_start, date_end=date_end
        )
    raise ValueError(f"no slot_periods_native handler for {strategy_id}")


def build_strategy_nav(
    conn: sqlite3.Connection,
    strategy_id: str,
    *,
    date_start: str,
    date_end: str,
    adapter: TrackAdapterSpec,
    comparison_notional_ntd: float,
    cost_model: dict | None = None,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    if date_end > full_dates[-1]:
        date_end = full_dates[-1]
    tdates = _trade_dates(full_dates, date_start, date_end)
    periods = collect_periods(
        conn,
        strategy_id,
        date_start=date_start,
        date_end=date_end,
        adapter=adapter,
    )
    close_panel = close.reindex(tdates)
    bench_ix = load_benchmark_close(conn, code=PROXY_CODE)
    if PROXY_CODE not in close_panel.columns:
        close_panel[PROXY_CODE] = bench_ix.reindex(tdates).astype(float)
    pm = portfolio_metrics_for_periods(
        conn,
        periods,
        tdates,
        total_capital=comparison_notional_ntd,
        n_slots=adapter.n_slots,
        close=close_panel,
        cost_model=cost_model,
        return_daily=True,
    )
    bench = load_benchmark_close(conn).reindex(close.index)
    bench_nav: list[dict[str, Any]] = []
    if not bench.empty and tdates:
        b0 = float(bench.loc[tdates[0]])
        for d in tdates:
            if d not in bench.index:
                continue
            px = float(bench.loc[d])
            bench_nav.append(
                {
                    "date": d,
                    "benchmark_equity_ntd": round(
                        comparison_notional_ntd * (px / b0 if b0 > 0 else 1.0),
                        2,
                    ),
                }
            )
    daily = pm.get("daily_nav") or []
    bench_by_date = {r["date"]: r["benchmark_equity_ntd"] for r in bench_nav}
    merged: list[dict[str, Any]] = []
    for row in daily:
        d = row["date"]
        merged.append(
            {
                "date": d,
                "strategy_id": strategy_id,
                "equity_ntd": row["equity_ntd"],
                "benchmark_equity_ntd": bench_by_date.get(d),
                "in_market_flag": row.get("in_market_flag", 0),
                "cash_pct": row.get("cash_pct"),
            }
        )
    pm["strategy_id"] = strategy_id
    pm["date_start"] = date_start
    pm["date_end"] = date_end
    pm["daily_nav_merged"] = merged
    pm["n_trading_days"] = len(tdates)
    pm["diagnostic_mean_excess_per_period"] = None
    return pm


def write_nav_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "date",
        "strategy_id",
        "equity_ntd",
        "benchmark_equity_ntd",
        "in_market_flag",
        "cash_pct",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def build_all_nav(
    conn: sqlite3.Connection | None = None,
    *,
    cfg: BacktestStandardConfig | None = None,
    window_id: str = "cmp_max_common",
) -> dict[str, dict[str, Any]]:
    own = conn is None
    if own:
        conn = connect(DEFAULT_DB_PATH)
    assert conn is not None
    cfg = cfg or load_backtest_standard_config()
    notional = cfg.comparison_notional_ntd
    win = cfg.windows.get(window_id) or {}
    date_start = str(win.get("date_start") or _COPYTRADE_EARLIEST)
    date_end = str(win.get("date_end") or date.today().isoformat())
    cost = cfg.cost_model if cfg.cost_model.get("enabled") else None
    out: dict[str, dict[str, Any]] = {}
    nav_dir = Path(cfg.output.get("nav_csv_dir") or "reports/research/_unified/")
    for sid, adapter in cfg.track_adapters.items():
        start = date_start
        if adapter.earliest_data and start < adapter.earliest_data:
            start = adapter.earliest_data
        result = build_strategy_nav(
            conn,
            sid,
            date_start=start,
            date_end=date_end,
            adapter=adapter,
            comparison_notional_ntd=notional,
            cost_model=cost,
        )
        write_nav_csv(result["daily_nav_merged"], nav_dir / f"{sid}_nav_daily.csv")
        out[sid] = result
    if own:
        conn.close()
    return out


def resolve_window_trade_range(
    full_dates: list[str],
    window: dict[str, Any],
    *,
    earliest_by_strategy: dict[str, str | None] | None = None,
) -> tuple[str, str]:
    if window.get("lookback_trading_days"):
        n = int(window["lookback_trading_days"])
        subset = full_dates[-n:] if len(full_dates) >= n else full_dates
        return subset[0], subset[-1]
    date_end = str(window.get("date_end") or full_dates[-1])
    date_start = str(window.get("date_start") or full_dates[0])
    if earliest_by_strategy:
        for ed in earliest_by_strategy.values():
            if ed and ed > date_start:
                date_start = ed
    if date_end > full_dates[-1]:
        date_end = full_dates[-1]
    return date_start, date_end
