"""Track 2 · Zarattini QQQ ORB replicated on TW 0050 (IR0002 daily benchmark)."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from research.backtest.archive.cz_intraday_common import (
    BENCHMARK_DAILY_CODE,
    DEFAULT_COST_RT,
    DEFAULT_LEVERAGE,
    INITIAL_AUM_NTD,
    TRACK2_OOS_MIN_N,
    TradeRecord,
    atr14,
    benchmark_buy_hold,
    filter_session,
    kbar_coverage,
    load_1m_frames,
    load_daily_bars,
    simulate_orb_qqq_session,
    split_dates_is_oos,
    summarize_trades,
    write_intraday_research_json,
)

DEFAULT_STOCK_ID = "0050"
VARIANTS: tuple[tuple[str, str, str], ...] = (
    ("range_eod", "range", "eod"),
    ("range_10r", "range", "10r"),
    ("atr5_eod", "atr5", "eod"),
    ("atr10_eod", "atr10", "eod"),
)


def run_track2_backtest(
    conn: sqlite3.Connection,
    *,
    stock_id: str = DEFAULT_STOCK_ID,
    cost_rt: float = DEFAULT_COST_RT,
    leverage: float = DEFAULT_LEVERAGE,
    initial_aum: float = INITIAL_AUM_NTD,
    allow_short: bool = True,
) -> dict[str, Any]:
    k1m = load_1m_frames(conn, stock_id)
    daily = load_daily_bars(conn, BENCHMARK_DAILY_CODE)
    if k1m.empty:
        return {
            "stock_id": stock_id,
            "error": "no_1m_data",
            "coverage": kbar_coverage(conn, stock_id),
        }

    dates = sorted(k1m["trade_date"].unique())
    split_date = split_dates_is_oos(dates)

    results: dict[str, Any] = {
        "topic": "cz-intraday-tw-replication",
        "track": "track2_0050_orb",
        "metrics_mode": "compounded_aum",
        "stock_id": stock_id,
        "benchmark_daily_code": BENCHMARK_DAILY_CODE,
        "spec": "Zarattini 2023 QQQ ORB · 5m range · 2nd bar open entry",
        "session": "09:00-13:30",
        "cost_rt_pct": round(cost_rt * 100, 3),
        "leverage_cap": leverage,
        "initial_aum_ntd": initial_aum,
        "split_date": split_date,
        "coverage": kbar_coverage(conn, stock_id),
        "variants": {},
    }

    trade_dates_with_orb: list[str] = []

    for label, stop_mode, exit_mode in VARIANTS:
        trades: list[TradeRecord] = []
        aum = initial_aum
        for td in dates:
            day = filter_session(k1m[k1m["trade_date"] == td])
            if day.empty:
                continue
            atr = atr14(daily, td) if stop_mode.startswith("atr") else None
            rec = simulate_orb_qqq_session(
                day,
                trade_date=td,
                atr14_val=atr,
                stop_mode=stop_mode,  # type: ignore[arg-type]
                exit_mode=exit_mode,  # type: ignore[arg-type]
                aum=aum,
                leverage=leverage,
                cost_rt=cost_rt,
                allow_short=allow_short,
            )
            if rec is None:
                continue
            trades.append(rec)
            if np.isfinite(rec.pnl_ntd):
                aum = max(0.0, aum + rec.pnl_ntd)
            if label == "range_eod":
                trade_dates_with_orb.append(td)

        results["variants"][label] = summarize_trades(
            trades,
            split_date=split_date,
            label=label,
            oos_min_n=TRACK2_OOS_MIN_N,
        )

    results["benchmark_bh_0050"] = benchmark_buy_hold(
        daily,
        trade_dates_with_orb or dates,
        split_date=split_date,
    )

    # Hypothesis H-CZ-T2-1: atr5_eod OOS alpha proxy via avg_net > 0
    primary = results["variants"].get("atr5_eod", {})
    oos = primary.get("oos", {})
    results["hypotheses"] = {
        "H-CZ-T2-1": {
            "variant": "atr5_eod",
            "oos_avg_net_pct": oos.get("avg_net_pct"),
            "oos_n": oos.get("n"),
            "pass": bool(oos.get("n", 0) >= TRACK2_OOS_MIN_N and (oos.get("avg_net_pct") or -99) > 0),
        },
        "H-CZ-T2-3": {
            "statement": "EOD exit Sharpe >= 10R exit Sharpe",
            "eod_sharpe": results["variants"].get("atr5_eod", {}).get("oos", {}).get("sharpe"),
            "r10_sharpe": results["variants"].get("range_10r", {}).get("oos", {}).get("sharpe"),
        },
    }

    return results


def write_track2_report(result: dict[str, Any], out_path: Path) -> None:
    write_intraday_research_json(result, out_path)
