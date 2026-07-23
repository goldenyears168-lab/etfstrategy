"""Load chip tables from local stocks.db into analysis frames.

Maps FinMind-backed tables onto schemas close to tw-institutional-stocker
InstitutionalFlow / MarginTrading fields (shares net, not vendor ORM).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from stock_db.util import DEFAULT_DB_PATH

SOURCE = "finmind"


def connect_ro(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or DEFAULT_DB_PATH).resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=120.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def load_institutional(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    d0: str,
    d1: str,
) -> pd.DataFrame:
    if not stock_ids:
        return pd.DataFrame()
    ph = ",".join("?" * len(stock_ids))
    df = pd.read_sql_query(
        f"""
        SELECT stock_id AS sid, trade_date AS d,
               foreign_net, investment_trust_net AS trust_net,
               dealer_self_net AS dealer_net, three_institution_net AS three_net
        FROM stock_institutional_daily
        WHERE source=? AND stock_id IN ({ph})
          AND trade_date>=? AND trade_date<=?
        ORDER BY stock_id, trade_date
        """,
        conn,
        params=[SOURCE, *stock_ids, d0, d1],
    )
    if df.empty:
        return df
    df["d"] = df["d"].astype(str)
    df["sid"] = df["sid"].astype(str)
    for c in ("foreign_net", "trust_net", "dealer_net", "three_net"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    return df


def load_margin(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    d0: str,
    d1: str,
) -> pd.DataFrame:
    if not stock_ids:
        return pd.DataFrame()
    ph = ",".join("?" * len(stock_ids))
    df = pd.read_sql_query(
        f"""
        SELECT stock_id AS sid, trade_date AS d,
               margin_balance, margin_change, short_balance, short_change
        FROM stock_margin_daily
        WHERE source=? AND stock_id IN ({ph})
          AND trade_date>=? AND trade_date<=?
        ORDER BY stock_id, trade_date
        """,
        conn,
        params=[SOURCE, *stock_ids, d0, d1],
    )
    if df.empty:
        return df
    df["d"] = df["d"].astype(str)
    df["sid"] = df["sid"].astype(str)
    for c in ("margin_balance", "margin_change", "short_balance", "short_change"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def load_lending(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    d0: str,
    d1: str,
) -> pd.DataFrame:
    if not stock_ids:
        return pd.DataFrame()
    ph = ",".join("?" * len(stock_ids))
    df = pd.read_sql_query(
        f"""
        SELECT stock_id AS sid, trade_date AS d,
               lending_balance, lending_change, fee_rate
        FROM stock_lending_daily
        WHERE source=? AND stock_id IN ({ph})
          AND trade_date>=? AND trade_date<=?
        ORDER BY stock_id, trade_date
        """,
        conn,
        params=[SOURCE, *stock_ids, d0, d1],
    )
    if df.empty:
        return df
    df["d"] = df["d"].astype(str)
    df["sid"] = df["sid"].astype(str)
    for c in ("lending_balance", "lending_change", "fee_rate"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def load_daytrade(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    d0: str,
    d1: str,
) -> pd.DataFrame:
    if not stock_ids:
        return pd.DataFrame()
    ph = ",".join("?" * len(stock_ids))
    df = pd.read_sql_query(
        f"""
        SELECT stock_id AS sid, trade_date AS d,
               daytrade_volume, total_volume, daytrade_ratio_pct
        FROM stock_daytrade_daily
        WHERE source=? AND stock_id IN ({ph})
          AND trade_date>=? AND trade_date<=?
        ORDER BY stock_id, trade_date
        """,
        conn,
        params=[SOURCE, *stock_ids, d0, d1],
    )
    if df.empty:
        return df
    df["d"] = df["d"].astype(str)
    df["sid"] = df["sid"].astype(str)
    for c in ("daytrade_volume", "total_volume", "daytrade_ratio_pct"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def load_shareholding(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    d0: str,
    d1: str,
) -> pd.DataFrame:
    """外資持股餘額／比例（TaiwanStockShareholding）。"""
    if not stock_ids:
        return pd.DataFrame()
    ph = ",".join("?" * len(stock_ids))
    df = pd.read_sql_query(
        f"""
        SELECT stock_id AS sid, trade_date AS d,
               foreign_remaining_shares, foreign_remaining_ratio, shares_issued
        FROM stock_shareholding_daily
        WHERE source=? AND stock_id IN ({ph})
          AND trade_date>=? AND trade_date<=?
        ORDER BY stock_id, trade_date
        """,
        conn,
        params=[SOURCE, *stock_ids, d0, d1],
    )
    if df.empty:
        return df
    df["d"] = df["d"].astype(str)
    df["sid"] = df["sid"].astype(str)
    for c in ("foreign_remaining_shares", "foreign_remaining_ratio", "shares_issued"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def load_institutional_side_wide(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    d0: str,
    d1: str,
) -> pd.DataFrame:
    """Pivot FinMind side names → columns (Foreign_Investor, Investment_Trust, …)."""
    if not stock_ids:
        return pd.DataFrame()
    ph = ",".join("?" * len(stock_ids))
    df = pd.read_sql_query(
        f"""
        SELECT stock_id AS sid, trade_date AS d, inst_name, net
        FROM stock_institutional_side_daily
        WHERE source=? AND stock_id IN ({ph})
          AND trade_date>=? AND trade_date<=?
        ORDER BY stock_id, trade_date
        """,
        conn,
        params=[SOURCE, *stock_ids, d0, d1],
    )
    if df.empty:
        return df
    df["d"] = df["d"].astype(str)
    df["sid"] = df["sid"].astype(str)
    df["net"] = pd.to_numeric(df["net"], errors="coerce").fillna(0.0)
    name_map = {
        "Foreign_Investor": "side_foreign",
        "Investment_Trust": "side_trust",
        "Dealer_self": "side_dealer_self",
        "Dealer_Hedging": "side_dealer_hedge",
        "Foreign_Dealer_Self": "side_foreign_dealer",
    }
    df["col"] = df["inst_name"].map(name_map)
    df = df.dropna(subset=["col"])
    wide = (
        df.pivot_table(index=["sid", "d"], columns="col", values="net", aggfunc="sum")
        .reset_index()
    )
    wide.columns.name = None
    for c in name_map.values():
        if c not in wide.columns:
            wide[c] = 0.0
        else:
            wide[c] = pd.to_numeric(wide[c], errors="coerce").fillna(0.0)
    return wide


def load_tx_foreign_futures(conn: sqlite3.Connection, d0: str, d1: str) -> pd.DataFrame:
    """TX futures · 外資 net OI (Chen foreign-futures chip layer)."""
    df = pd.read_sql_query(
        """
        SELECT trade_date AS d, net_oi_vol, long_oi_vol, short_oi_vol,
               net_deal_vol
        FROM futures_institutional_daily
        WHERE source=? AND futures_id='TX' AND inst_name='外資'
          AND trade_date>=? AND trade_date<=?
        ORDER BY trade_date
        """,
        conn,
        params=[SOURCE, d0, d1],
    )
    if df.empty:
        return df
    df["d"] = df["d"].astype(str)
    for c in ("net_oi_vol", "long_oi_vol", "short_oi_vol", "net_deal_vol"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def load_ohlc(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    d0: str,
    d1: str,
) -> pd.DataFrame:
    if not stock_ids:
        return pd.DataFrame()
    ph = ",".join("?" * len(stock_ids))
    df = pd.read_sql_query(
        f"""
        SELECT stock_id AS sid, trade_date AS d, open, close, volume
        FROM stock_daily_bars
        WHERE source=? AND stock_id IN ({ph})
          AND trade_date>=? AND trade_date<=date(?, '+40 day')
          AND open>0 AND close>0
        ORDER BY stock_id, trade_date
        """,
        conn,
        params=[SOURCE, *stock_ids, d0, d1],
    )
    if df.empty:
        return df
    df["d"] = df["d"].astype(str)
    df["sid"] = df["sid"].astype(str)
    return df


def load_bench(conn: sqlite3.Connection, d0: str, d1: str) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT date, close FROM daily_bars
        WHERE code='IX0001' AND date>=? AND date<=date(?, '+40 day')
        ORDER BY date
        """,
        (d0, d1),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            """
            SELECT trade_date, close FROM stock_daily_bars
            WHERE stock_id='IX0001' AND source=? AND trade_date>=?
            ORDER BY trade_date
            """,
            (SOURCE, d0),
        ).fetchall()
    return {str(a): float(b) for a, b in rows if b}


def load_calendar(conn: sqlite3.Connection, d0: str, d1: str) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(
            """
            SELECT DISTINCT trade_date FROM stock_daily_bars
            WHERE source=? AND trade_date>=? AND trade_date<=date(?, '+40 day')
            ORDER BY trade_date
            """,
            (SOURCE, d0, d1),
        )
    ]
