"""SQLite storage for ETF daily sync (Phase 0 local).

Tables:
  daily_bars                  — TEJ ETF/index OHLCV (5 ETFs + IX0001 + IR0002)
  etf_daily_signal_snapshot   — FinMind close + 三大法人
  etf_holdings / meta         — EZMoney (統一 3 檔) + KGIFund (凱基 009816/00407A)
  latest_quotes               — legacy schema; daily sync no longer writes
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path("data/stocks.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bars (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL NOT NULL,
    volume INTEGER,
    spread REAL,
    source TEXT NOT NULL DEFAULT 'finmind',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (code, date, source)
);

CREATE TABLE IF NOT EXISTS latest_quotes (
    code TEXT NOT NULL,
    name TEXT,
    market TEXT,
    date TEXT,
    close REAL,
    change REAL,
    change_pct REAL,
    volume INTEGER,
    source TEXT NOT NULL,
    queried_at TEXT NOT NULL,
    PRIMARY KEY (code, source)
);

CREATE TABLE IF NOT EXISTS etf_holdings_meta (
    etf_code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    nav REAL,
    holding_count INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'ezmoney',
    source_edit_at TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (etf_code, snapshot_date)
);

CREATE TABLE IF NOT EXISTS etf_holdings (
    etf_code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT,
    shares REAL NOT NULL,
    weight_pct REAL,
    amount REAL,
    source TEXT NOT NULL DEFAULT 'ezmoney',
    source_edit_at TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (etf_code, snapshot_date, stock_id)
);

CREATE INDEX IF NOT EXISTS idx_etf_holdings_date
    ON etf_holdings (etf_code, snapshot_date);

CREATE TABLE IF NOT EXISTS etf_daily_signal_snapshot (
    code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    close_price REAL,
    foreign_net REAL,
    investment_trust_net REAL,
    dealer_self_net REAL,
    three_institution_net REAL,
    source TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (code, snapshot_date, source)
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def upsert_daily_bars(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO daily_bars (
            code, date, open, high, low, close, volume, spread, source, synced_at
        ) VALUES (
            :code, :date, :open, :high, :low, :close, :volume, :spread, :source, :synced_at
        )
        ON CONFLICT(code, date, source) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume, spread=excluded.spread,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_latest_quotes(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO latest_quotes (
            code, name, market, date, close, change, change_pct, volume, source, queried_at
        ) VALUES (
            :code, :name, :market, :date, :close, :change, :change_pct, :volume, :source, :queried_at
        )
        ON CONFLICT(code, source) DO UPDATE SET
            name=excluded.name, market=excluded.market, date=excluded.date,
            close=excluded.close, change=excluded.change, change_pct=excluded.change_pct,
            volume=excluded.volume, queried_at=excluded.queried_at
    """
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


def load_latest_comparison(conn: sqlite3.Connection, codes: list[str]) -> list[sqlite3.Row]:
    placeholders = ",".join("?" * len(codes))
    sql = f"""
        SELECT code, name, market, source, date, close, change, change_pct, volume, queried_at
        FROM latest_quotes
        WHERE code IN ({placeholders})
        ORDER BY code, source
    """
    return list(conn.execute(sql, codes))


def upsert_etf_holdings_meta(conn: sqlite3.Connection, row: dict) -> None:
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO etf_holdings_meta (
            etf_code, snapshot_date, nav, holding_count, source, source_edit_at, synced_at
        ) VALUES (
            :etf_code, :snapshot_date, :nav, :holding_count, :source, :source_edit_at, :synced_at
        )
        ON CONFLICT(etf_code, snapshot_date) DO UPDATE SET
            nav=excluded.nav,
            holding_count=excluded.holding_count,
            source=excluded.source,
            source_edit_at=excluded.source_edit_at,
            synced_at=excluded.synced_at
    """
    conn.execute(sql, {**row, "synced_at": synced_at})
    conn.commit()


def upsert_etf_holdings(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO etf_holdings (
            etf_code, snapshot_date, stock_id, stock_name, shares, weight_pct, amount,
            source, source_edit_at, synced_at
        ) VALUES (
            :etf_code, :snapshot_date, :stock_id, :stock_name, :shares, :weight_pct, :amount,
            :source, :source_edit_at, :synced_at
        )
        ON CONFLICT(etf_code, snapshot_date, stock_id) DO UPDATE SET
            stock_name=excluded.stock_name,
            shares=excluded.shares,
            weight_pct=excluded.weight_pct,
            amount=excluded.amount,
            source=excluded.source,
            source_edit_at=excluded.source_edit_at,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def list_etf_snapshot_dates(conn: sqlite3.Connection, etf_code: str) -> list[str]:
    sql = """
        SELECT snapshot_date
        FROM etf_holdings_meta
        WHERE etf_code = ?
        ORDER BY snapshot_date DESC
    """
    return [row[0] for row in conn.execute(sql, (etf_code,))]


def load_etf_holdings_meta(
    conn: sqlite3.Connection,
    etf_code: str,
    snapshot_date: str,
) -> sqlite3.Row | None:
    sql = """
        SELECT etf_code, snapshot_date, nav, holding_count, source, source_edit_at, synced_at
        FROM etf_holdings_meta
        WHERE etf_code = ? AND snapshot_date = ?
    """
    row = conn.execute(sql, (etf_code, snapshot_date)).fetchone()
    return row


def load_etf_holdings(
    conn: sqlite3.Connection,
    etf_code: str,
    snapshot_date: str,
) -> list[sqlite3.Row]:
    sql = """
        SELECT etf_code, snapshot_date, stock_id, stock_name, shares, weight_pct, amount,
               source, source_edit_at, synced_at
        FROM etf_holdings
        WHERE etf_code = ? AND snapshot_date = ?
        ORDER BY stock_id
    """
    return list(conn.execute(sql, (etf_code, snapshot_date)))


def compute_etf_holdings_changes(
    conn: sqlite3.Connection,
    etf_code: str,
    curr_date: str | None = None,
    prev_date: str | None = None,
) -> list[sqlite3.Row]:
    dates = list_etf_snapshot_dates(conn, etf_code)
    if not dates:
        return []
    if curr_date is None:
        curr_date = dates[0]
    if prev_date is None:
        if len(dates) < 2:
            return []
        prev_date = dates[1] if dates[0] == curr_date else dates[0]

    sql = """
        WITH curr AS (
            SELECT stock_id, stock_name, shares, weight_pct
            FROM etf_holdings
            WHERE etf_code = ? AND snapshot_date = ?
        ),
        prev AS (
            SELECT stock_id, stock_name, shares, weight_pct
            FROM etf_holdings
            WHERE etf_code = ? AND snapshot_date = ?
        )
        SELECT
            COALESCE(c.stock_id, p.stock_id) AS stock_id,
            COALESCE(c.stock_name, p.stock_name) AS stock_name,
            p.shares AS shares_prev,
            c.shares AS shares_curr,
            COALESCE(c.shares, 0) - COALESCE(p.shares, 0) AS share_delta,
            COALESCE(c.weight_pct, 0) - COALESCE(p.weight_pct, 0) AS weight_delta,
            CASE
                WHEN p.stock_id IS NULL THEN '新进'
                WHEN c.stock_id IS NULL THEN '出清'
                WHEN c.shares > p.shares THEN '加码'
                WHEN c.shares < p.shares THEN '减码'
                ELSE '不变'
            END AS action
        FROM curr c
        FULL OUTER JOIN prev p ON c.stock_id = p.stock_id
        ORDER BY stock_id
    """
    return list(conn.execute(sql, (etf_code, curr_date, etf_code, prev_date)))


def upsert_etf_daily_signal_snapshots(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO etf_daily_signal_snapshot (
            code, snapshot_date, close_price, foreign_net, investment_trust_net,
            dealer_self_net, three_institution_net, source, synced_at
        ) VALUES (
            :code, :snapshot_date, :close_price, :foreign_net, :investment_trust_net,
            :dealer_self_net, :three_institution_net, :source, :synced_at
        )
        ON CONFLICT(code, snapshot_date, source) DO UPDATE SET
            close_price=excluded.close_price,
            foreign_net=excluded.foreign_net,
            investment_trust_net=excluded.investment_trust_net,
            dealer_self_net=excluded.dealer_self_net,
            three_institution_net=excluded.three_institution_net,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)
