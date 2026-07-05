"""FinMind marketplace · 突破月線長紅放量 · shared PIT helpers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from research.backtest.finpilot_local_backtest import load_price_panels
from screener_universe import resolve_sync_watchlist
from stock_db import DEFAULT_DB_PATH, connect

TOPIC_MA20_BREAKOUT = "finmind-ma20-breakout-volume"
RETURN_1D_MIN_PCT = 5.0
VOL_RATIO_MIN = 2.0
DEFAULT_DATE_START = "2015-01-01"


@dataclass(frozen=True)
class Ma20BreakoutParams:
    return_1d_min_pct: float = RETURN_1D_MIN_PCT
    vol_ratio_min: float = VOL_RATIO_MIN
    strict_ma20_breakout: bool = True


def resolve_universe_stock_ids(conn: sqlite3.Connection, universe: str) -> list[str]:
    rows = resolve_sync_watchlist(conn, universe)
    return sorted({str(r["stock_id"]) for r in rows})


def load_technical_breakout_long(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT stock_id, trade_date, return_1d_pct, ma20, vol_ratio_5d
        FROM stock_technical_daily
        WHERE source = 'computed'
        ORDER BY stock_id, trade_date
        """
    ).fetchall()
    if not rows:
        return pd.DataFrame(
            columns=["stock_id", "trade_date", "return_1d_pct", "ma20", "vol_ratio_5d"]
        )
    return pd.DataFrame(
        rows,
        columns=["stock_id", "trade_date", "return_1d_pct", "ma20", "vol_ratio_5d"],
    )


def _wide_from_long(df: pd.DataFrame, col: str, stock_ids: list[str], cal: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(index=cal, columns=stock_ids, dtype=float)
    sub = df[df["stock_id"].isin(stock_ids) & df["trade_date"].isin(cal)]
    if sub.empty:
        return pd.DataFrame(index=cal, columns=stock_ids, dtype=float)
    pivot = sub.pivot_table(index="trade_date", columns="stock_id", values=col, aggfunc="last")
    return pivot.reindex(index=cal, columns=stock_ids)


def _build_ma20_breakout_matrix(
    close: pd.DataFrame,
    ma20: pd.DataFrame,
    *,
    strict: bool,
) -> pd.DataFrame:
    if strict:
        above = close > ma20
        prev_below = close.shift(1) <= ma20.shift(1)
        return (above & prev_below).fillna(False).astype(bool)
    return (close > ma20).fillna(False).astype(bool)


@dataclass(frozen=True)
class Ma20BreakoutScreenMatrices:
    ma20_breakout: pd.DataFrame
    return_1d: pd.DataFrame
    vol_ratio: pd.DataFrame
    return_1d_val: pd.DataFrame


def build_ma20_breakout_screen_matrices(
    *,
    stock_ids: list[str],
    cal: list[str],
    close: pd.DataFrame,
    tech: pd.DataFrame,
    params: Ma20BreakoutParams | None = None,
) -> Ma20BreakoutScreenMatrices:
    p = params or Ma20BreakoutParams()
    price_cols = [s for s in stock_ids if s in close.columns]
    close_w = close.reindex(cal)[price_cols].reindex(columns=stock_ids)
    ma20_w = _wide_from_long(tech, "ma20", stock_ids, cal)
    ret_w = _wide_from_long(tech, "return_1d_pct", stock_ids, cal)
    vol_w = _wide_from_long(tech, "vol_ratio_5d", stock_ids, cal)

    breakout = _build_ma20_breakout_matrix(close_w, ma20_w, strict=p.strict_ma20_breakout)
    ret_ok = ret_w.ge(p.return_1d_min_pct).fillna(False).astype(bool)
    vol_ok = vol_w.ge(p.vol_ratio_min).fillna(False).astype(bool)

    return Ma20BreakoutScreenMatrices(
        ma20_breakout=breakout,
        return_1d=ret_ok,
        vol_ratio=vol_ok,
        return_1d_val=ret_w,
    )


def count_condition_passes(
    conn: sqlite3.Connection,
    universe: str,
    date_start: str,
    date_end: str,
    *,
    params: Ma20BreakoutParams | None = None,
) -> dict[str, Any]:
    p = params or Ma20BreakoutParams()
    stock_ids = resolve_universe_stock_ids(conn, universe)
    close, _, _ = load_price_panels(conn)
    cal = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]
    tech = load_technical_breakout_long(conn)
    tech = tech[tech["stock_id"].isin(stock_ids)]

    matrices = build_ma20_breakout_screen_matrices(
        stock_ids=stock_ids,
        cal=cal,
        close=close,
        tech=tech,
        params=p,
    )
    price_ok = [s for s in stock_ids if s in close.columns]

    counts = {
        "p1_ma20_breakout": 0,
        "p2_return_5pct": 0,
        "i1_vol_ratio_2x": 0,
        "full_screen": 0,
    }
    by_year: dict[str, int] = {}

    for d in cal:
        p1 = matrices.ma20_breakout.loc[d, price_ok]
        p2 = matrices.return_1d.loc[d, price_ok]
        i1 = matrices.vol_ratio.loc[d, price_ok]
        counts["p1_ma20_breakout"] += int(p1.sum())
        counts["p2_return_5pct"] += int(p2.sum())
        counts["i1_vol_ratio_2x"] += int(i1.sum())
        full = p1 & p2 & i1
        day_full = int(full.sum())
        counts["full_screen"] += day_full
        if day_full:
            by_year[d[:4]] = by_year.get(d[:4], 0) + day_full

    table_rows = conn.execute(
        """
        SELECT 'technical', COUNT(DISTINCT stock_id), MIN(trade_date), MAX(trade_date)
        FROM stock_technical_daily
        UNION ALL
        SELECT 'daily_bars', COUNT(DISTINCT stock_id), MIN(trade_date), MAX(trade_date)
        FROM stock_daily_bars WHERE source='finmind'
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
            "i1_volume": "vol_ratio_5d daily proxy for intraday 2x estimate",
            "p1_breakout": "strict close>ma20 with prior close<=ma20"
            if p.strict_ma20_breakout
            else "loose close>ma20",
        },
        "phase0_pass": counts["full_screen"] >= 50,
    }


def validate_ma20_breakout_data(
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
