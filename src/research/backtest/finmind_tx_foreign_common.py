"""FinMind marketplace · TX 外資淨多單 3 日 · signal helpers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from market_benchmark import load_benchmark_close
from stock_db import DEFAULT_DB_PATH, connect

TOPIC_TX_FOREIGN = "finmind-tx-foreign-net-long-3d"
FUTURES_ID = "TX"
FOREIGN_INST_NAMES = ("外資", "Foreign_Investor")
STREAK_DAYS = 3
MAX_HOLD_DAYS = 60
PROXY_CODE = "IX0001"
DEFAULT_DATE_START = "2018-06-05"


@dataclass(frozen=True)
class TxForeignParams:
    futures_id: str = FUTURES_ID
    streak_days: int = STREAK_DAYS
    max_hold_days: int = MAX_HOLD_DAYS
    proxy_code: str = PROXY_CODE
    ma_exit_period: int = 5


def load_foreign_net_oi(conn: sqlite3.Connection, futures_id: str = FUTURES_ID) -> pd.Series:
    placeholders = ",".join("?" for _ in FOREIGN_INST_NAMES)
    rows = conn.execute(
        f"""
        SELECT trade_date, net_oi_vol
        FROM futures_institutional_daily
        WHERE futures_id = ? AND inst_name IN ({placeholders}) AND source = 'finmind'
        ORDER BY trade_date
        """,
        (futures_id, *FOREIGN_INST_NAMES),
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows, columns=["trade_date", "net_oi_vol"])
    df = df.drop_duplicates(subset=["trade_date"], keep="last")
    return df.set_index("trade_date")["net_oi_vol"].astype(float)


def streak_signals(net: pd.Series, *, streak_days: int = STREAK_DAYS) -> pd.Series:
    if net.empty:
        return pd.Series(dtype=bool)
    pos = net > 0
    roll = pos.rolling(streak_days, min_periods=streak_days).sum()
    return (roll >= streak_days).fillna(False).astype(bool)


def load_proxy_close_ma(
    conn: sqlite3.Connection,
    cal: list[str],
    *,
    code: str = PROXY_CODE,
    ma_period: int = 5,
) -> tuple[pd.Series, pd.Series]:
    bench = load_benchmark_close(conn, code=code)
    if bench.empty:
        empty = pd.Series(dtype=float)
        return empty, empty
    close = bench.reindex(cal).astype(float)
    ma = close.rolling(ma_period, min_periods=ma_period).mean()
    return close, ma


def bench_open(conn: sqlite3.Connection, trade_date: str, *, code: str = PROXY_CODE) -> float | None:
    row = conn.execute(
        """
        SELECT open, close FROM daily_bars
        WHERE code = ? AND date = ?
        ORDER BY CASE source WHEN 'tej' THEN 0 WHEN 'finmind' THEN 1 WHEN 'yahoo' THEN 2 ELSE 3 END
        LIMIT 1
        """,
        (code, trade_date),
    ).fetchone()
    if row is None:
        return None
    if row["open"] is not None and float(row["open"]) > 0:
        return float(row["open"])
    if row["close"] is not None:
        return float(row["close"])
    return None


def bench_close(conn: sqlite3.Connection, trade_date: str, *, code: str = PROXY_CODE) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM daily_bars
        WHERE code = ? AND date = ?
        ORDER BY CASE source WHEN 'tej' THEN 0 WHEN 'finmind' THEN 1 WHEN 'yahoo' THEN 2 ELSE 3 END
        LIMIT 1
        """,
        (code, trade_date),
    ).fetchone()
    if row is None or row["close"] is None:
        return None
    return float(row["close"])


def count_futures_gaps(conn: sqlite3.Connection, futures_id: str, cal: list[str]) -> dict[str, Any]:
    fut_dates = {
        str(r[0])
        for r in conn.execute(
            """
            SELECT DISTINCT trade_date FROM futures_institutional_daily
            WHERE futures_id = ? AND inst_name IN (?, ?)
            """,
            (futures_id, *FOREIGN_INST_NAMES),
        ).fetchall()
    }
    in_range = [d for d in cal if d >= DEFAULT_DATE_START]
    missing = [d for d in in_range if d not in fut_dates]
    return {
        "calendar_days_in_range": len(in_range),
        "futures_days_present": len(in_range) - len(missing),
        "missing_days": len(missing),
        "missing_sample": missing[:10],
    }


def validate_tx_foreign_data(
    db_path: str | Path | None = None,
    *,
    date_start: str = DEFAULT_DATE_START,
    date_end: str | None = None,
    params: TxForeignParams | None = None,
) -> dict[str, Any]:
    from datetime import date as dt

    p = params or TxForeignParams()
    end = date_end or dt.today().isoformat()
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    conn = connect(path)
    try:
        bench = load_benchmark_close(conn, code=p.proxy_code)
        cal = [d for d in bench.index.astype(str).tolist() if date_start <= d <= end]
        net = load_foreign_net_oi(conn, p.futures_id)
        net = net.reindex(cal)
        signals = streak_signals(net.dropna(), streak_days=p.streak_days)
        signals = signals.reindex(cal).fillna(False)

        gaps = count_futures_gaps(conn, p.futures_id, cal)
        pos_days = int((net > 0).sum())
        streak_hits = int(signals.sum())

        return {
            "futures_id": p.futures_id,
            "proxy_code": p.proxy_code,
            "date_start": date_start,
            "date_end": end,
            "trading_days": len(cal),
            "foreign_oi_days": int(net.notna().sum()),
            "foreign_net_positive_days": pos_days,
            "e1_streak_3d_signals": streak_hits,
            "futures_coverage": gaps,
            "approx_notes": {
                "entry": f"{p.futures_id} foreign net_oi_vol>0 x{p.streak_days}d",
                "exit": f"{p.proxy_code} close<MA{p.ma_exit_period} -> T+1 open flat",
                "trade_proxy": f"long {p.proxy_code} open-to-open (single-position timing)",
                "e2_holdings": "N/A · futures directional",
            },
            "phase0_pass": streak_hits >= 30 and gaps["missing_days"] <= 500,
        }
    finally:
        conn.close()
