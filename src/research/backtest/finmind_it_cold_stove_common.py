"""FinMind marketplace · 投信燒冷灶（簡化版）· shared helpers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from research.backtest.finmind_screener_common import load_institutional_long, resolve_universe_stock_ids
from research.backtest.finpilot_local_backtest import load_price_panels
from stock_db import DEFAULT_DB_PATH, connect

TOPIC_IT_COLD_STOVE = "finmind-it-cold-stove"
IT_BUY_MIN = 300.0
DEFAULT_STOP_LOSS_PCT = 10.0
DEFAULT_TAKE_PROFIT_PCT = 15.0
DEFAULT_MAX_HOLD_DAYS = 40
DEFAULT_DATE_START = "2015-01-01"


@dataclass(frozen=True)
class ItColdStoveParams:
    it_buy_min: float = IT_BUY_MIN
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT
    take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT
    max_hold_days: int = DEFAULT_MAX_HOLD_DAYS


@dataclass(frozen=True)
class ItColdStoveScreenMatrices:
    it_buy: pd.DataFrame
    it_net: pd.DataFrame


def build_it_cold_stove_screen_matrices(
    *,
    stock_ids: list[str],
    cal: list[str],
    inst: pd.DataFrame,
    params: ItColdStoveParams | None = None,
) -> ItColdStoveScreenMatrices:
    p = params or ItColdStoveParams()
    sub = inst[inst["stock_id"].isin(stock_ids) & inst["trade_date"].isin(cal)]
    if sub.empty:
        empty = pd.DataFrame(False, index=cal, columns=stock_ids)
        return ItColdStoveScreenMatrices(it_buy=empty, it_net=empty.astype(float))
    net = sub.pivot_table(
        index="trade_date", columns="stock_id", values="investment_trust_net", aggfunc="last"
    ).reindex(index=cal, columns=stock_ids)
    it_buy = net.gt(p.it_buy_min).fillna(False).astype(bool)
    return ItColdStoveScreenMatrices(it_buy=it_buy, it_net=net)


def count_condition_passes(
    conn: sqlite3.Connection,
    universe: str,
    date_start: str,
    date_end: str,
    *,
    params: ItColdStoveParams | None = None,
) -> dict[str, Any]:
    p = params or ItColdStoveParams()
    stock_ids = resolve_universe_stock_ids(conn, universe)
    close, _, _ = load_price_panels(conn)
    cal = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]
    inst = load_institutional_long(conn)
    inst = inst[inst["stock_id"].isin(stock_ids)]

    matrices = build_it_cold_stove_screen_matrices(
        stock_ids=stock_ids, cal=cal, inst=inst, params=p
    )
    price_ok = [s for s in stock_ids if s in close.columns]

    counts = {"e1_it_buy": 0, "e2_holdings_blocked": 0, "full_screen": 0}
    by_year: dict[str, int] = {}

    for d in cal:
        e1 = matrices.it_buy.loc[d, price_ok]
        day_n = int(e1.sum())
        counts["e1_it_buy"] += day_n
        counts["full_screen"] += day_n
        if day_n:
            by_year[d[:4]] = by_year.get(d[:4], 0) + day_n

    table_rows = conn.execute(
        """
        SELECT 'institutional', COUNT(DISTINCT stock_id), MIN(trade_date), MAX(trade_date)
        FROM stock_institutional_daily
        """
    ).fetchall()

    return {
        "universe": universe,
        "universe_size": len(stock_ids),
        "date_start": date_start,
        "date_end": date_end,
        "trading_days": len(cal),
        "condition_hits": counts,
        "full_screen_by_year": dict(sorted(by_year.items())),
        "table_coverage": [
            {"table": r[0], "stocks": r[1], "min_date": r[2], "max_date": r[3]}
            for r in table_rows
        ],
        "approx_notes": {
            "e2_holdings": "blocked · no IT shareholding dataset",
            "fidelity": "simplified · E1 only",
            "exit": f"SL {p.stop_loss_pct}% / TP {p.take_profit_pct}% / max {p.max_hold_days}d · OHLC",
        },
        "phase0_pass": counts["full_screen"] >= 50,
    }


def validate_it_cold_stove_data(
    db_path: str | Path | None = None,
    *,
    universe: str = "tw100",
    date_start: str = DEFAULT_DATE_START,
    date_end: str | None = None,
) -> dict[str, Any]:
    from datetime import date as dt

    end = date_end or dt.today().isoformat()
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    conn = connect(path)
    try:
        return count_condition_passes(conn, universe, date_start, end)
    finally:
        conn.close()
