"""SQLite storage for ETF daily sync (Phase 0 local).

Tables:
  daily_bars                  — TEJ ETF/index OHLCV (4 ETFs + IX0001 + IR0002)
  etf_daily_signal_snapshot   — FinMind close + 三大法人
  etf_holdings / meta         — EZMoney (統一 2 檔) + KGIFund (凱基 009816/00407A)
  stock_beta                  — 上市櫃 Beta vs ^TWII（sync_stock_beta.py，小數 2 位）
  tech_risk_daily_snapshot    — 科技風險三層（TSM ADR / SOX / 台指期 gap，sync_tech_risk_context.py）
  intraday_1m_bars            — 盤中 1 分 K 與特徵（intraday_monitor.py）
  intraday_signals            — 每分鐘 buy_signal 快照
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# 專案根目錄（src/ 的上一層）；腳本一律在根目錄 cwd 執行，路徑仍以此為準
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "stocks.db"

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

CREATE TABLE IF NOT EXISTS stock_beta (
    stock_id TEXT NOT NULL,
    name TEXT,
    market TEXT NOT NULL,
    beta REAL,
    beta_window TEXT NOT NULL,
    benchmark TEXT,
    source TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, source, beta_window)
);

CREATE INDEX IF NOT EXISTS idx_stock_beta_market
    ON stock_beta (market, stock_id);

CREATE TABLE IF NOT EXISTS tech_risk_daily_snapshot (
    session_date TEXT NOT NULL,
    us_trade_date TEXT,
    tsm_close REAL,
    tsm_daily_return_pct REAL,
    tsm_ma5 REAL,
    tsm_ma10 REAL,
    tsm_vs_ma5_pct REAL,
    tsm_vs_ma10_pct REAL,
    tsm_above_ma5 INTEGER,
    tsm_above_ma10 INTEGER,
    sox_close REAL,
    sox_daily_return_pct REAL,
    sox_ma5 REAL,
    sox_above_ma5 INTEGER,
    smh_close REAL,
    smh_daily_return_pct REAL,
    semi_benchmark TEXT NOT NULL DEFAULT 'SOX',
    tw_spot_date TEXT,
    tw_spot_code TEXT NOT NULL DEFAULT 'IX0001',
    tw_spot_prev_close REAL,
    tx_futures_id TEXT,
    tx_contract_date TEXT,
    tx_futures_price REAL,
    tx_futures_session TEXT,
    tx_gap_pct REAL,
    te_futures_id TEXT,
    te_contract_date TEXT,
    te_futures_price REAL,
    te_futures_session TEXT,
    te_overnight_pct REAL,
    notes TEXT,
    source_us TEXT NOT NULL DEFAULT 'yahoo',
    source_tw TEXT NOT NULL DEFAULT 'finmind',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (session_date)
);

CREATE TABLE IF NOT EXISTS intraday_1m_bars (
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    open_1m REAL,
    high_1m REAL,
    low_1m REAL,
    close_1m REAL,
    volume_1m REAL,
    cum_volume REAL,
    vwap_day REAL,
    day_return REAL,
    rel_volume_est REAL,
    ret_vs_index_day REAL,
    order_imbalance_1 REAL,
    position_in_day_range REAL,
    breakout_flag INTEGER,
    source TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (symbol, ts, source)
);

CREATE TABLE IF NOT EXISTS intraday_signals (
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    buy_signal INTEGER NOT NULL,
    etf_hold_count INTEGER,
    rel_volume_est REAL,
    ret_vs_index_day REAL,
    position_in_day_range REAL,
    order_imbalance_1 REAL,
    breakout_flag INTEGER,
    reason TEXT,
    source TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (symbol, ts, source)
);

CREATE INDEX IF NOT EXISTS idx_intraday_signals_ts
    ON intraday_signals (ts, buy_signal);
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
            p.weight_pct AS weight_pct_prev,
            c.weight_pct AS weight_pct_curr,
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


def upsert_tech_risk_daily_snapshots(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO tech_risk_daily_snapshot (
            session_date, us_trade_date,
            tsm_close, tsm_daily_return_pct, tsm_ma5, tsm_ma10,
            tsm_vs_ma5_pct, tsm_vs_ma10_pct, tsm_above_ma5, tsm_above_ma10,
            sox_close, sox_daily_return_pct, sox_ma5, sox_above_ma5,
            smh_close, smh_daily_return_pct, semi_benchmark,
            tw_spot_date, tw_spot_code, tw_spot_prev_close,
            tx_futures_id, tx_contract_date, tx_futures_price, tx_futures_session, tx_gap_pct,
            te_futures_id, te_contract_date, te_futures_price, te_futures_session, te_overnight_pct,
            notes, source_us, source_tw, synced_at
        ) VALUES (
            :session_date, :us_trade_date,
            :tsm_close, :tsm_daily_return_pct, :tsm_ma5, :tsm_ma10,
            :tsm_vs_ma5_pct, :tsm_vs_ma10_pct, :tsm_above_ma5, :tsm_above_ma10,
            :sox_close, :sox_daily_return_pct, :sox_ma5, :sox_above_ma5,
            :smh_close, :smh_daily_return_pct, :semi_benchmark,
            :tw_spot_date, :tw_spot_code, :tw_spot_prev_close,
            :tx_futures_id, :tx_contract_date, :tx_futures_price, :tx_futures_session, :tx_gap_pct,
            :te_futures_id, :te_contract_date, :te_futures_price, :te_futures_session, :te_overnight_pct,
            :notes, :source_us, :source_tw, :synced_at
        )
        ON CONFLICT(session_date) DO UPDATE SET
            us_trade_date=excluded.us_trade_date,
            tsm_close=excluded.tsm_close,
            tsm_daily_return_pct=excluded.tsm_daily_return_pct,
            tsm_ma5=excluded.tsm_ma5, tsm_ma10=excluded.tsm_ma10,
            tsm_vs_ma5_pct=excluded.tsm_vs_ma5_pct, tsm_vs_ma10_pct=excluded.tsm_vs_ma10_pct,
            tsm_above_ma5=excluded.tsm_above_ma5, tsm_above_ma10=excluded.tsm_above_ma10,
            sox_close=excluded.sox_close, sox_daily_return_pct=excluded.sox_daily_return_pct,
            sox_ma5=excluded.sox_ma5, sox_above_ma5=excluded.sox_above_ma5,
            smh_close=excluded.smh_close, smh_daily_return_pct=excluded.smh_daily_return_pct,
            semi_benchmark=excluded.semi_benchmark,
            tw_spot_date=excluded.tw_spot_date, tw_spot_code=excluded.tw_spot_code,
            tw_spot_prev_close=excluded.tw_spot_prev_close,
            tx_futures_id=excluded.tx_futures_id, tx_contract_date=excluded.tx_contract_date,
            tx_futures_price=excluded.tx_futures_price, tx_futures_session=excluded.tx_futures_session,
            tx_gap_pct=excluded.tx_gap_pct,
            te_futures_id=excluded.te_futures_id, te_contract_date=excluded.te_contract_date,
            te_futures_price=excluded.te_futures_price, te_futures_session=excluded.te_futures_session,
            te_overnight_pct=excluded.te_overnight_pct,
            notes=excluded.notes,
            source_us=excluded.source_us, source_tw=excluded.source_tw,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_beta(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_beta (
            stock_id, name, market, beta, beta_window, benchmark, source, as_of_date, synced_at
        ) VALUES (
            :stock_id, :name, :market, :beta, :beta_window, :benchmark, :source, :as_of_date, :synced_at
        )
        ON CONFLICT(stock_id, source, beta_window) DO UPDATE SET
            name=excluded.name,
            market=excluded.market,
            beta=excluded.beta,
            benchmark=excluded.benchmark,
            as_of_date=excluded.as_of_date,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_stock_beta_map(
    conn: sqlite3.Connection,
) -> tuple[dict[str, sqlite3.Row], str | None]:
    """Latest stock_beta rows keyed by stock_id. Empty if table missing or empty."""
    try:
        as_of_row = conn.execute(
            "SELECT MAX(as_of_date) AS d FROM stock_beta"
        ).fetchone()
    except sqlite3.OperationalError:
        return {}, None
    if as_of_row is None or as_of_row["d"] is None:
        return {}, None
    as_of_date = as_of_row["d"]
    rows = conn.execute(
        """
        SELECT stock_id, beta, beta_window, source, as_of_date
        FROM stock_beta
        WHERE as_of_date = ?
        """,
        (as_of_date,),
    ).fetchall()
    return {row["stock_id"]: row for row in rows}, as_of_date


ETF_CODES_INTRADAY_DEFAULT = (
    "00981A",
    "00403A",
    "009816",
    "00980A",
    "00982A",
    "00992A",
)


def load_etf_constituent_watchlist(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...] = ETF_CODES_INTRADAY_DEFAULT,
) -> list[dict]:
    """最新各 ETF snapshot 持股聯集（日內監控 universe）。"""
    if not etf_codes:
        return []
    placeholders = ",".join("?" * len(etf_codes))
    sql = f"""
        WITH latest AS (
            SELECT etf_code, MAX(snapshot_date) AS snapshot_date
            FROM etf_holdings_meta
            WHERE etf_code IN ({placeholders})
            GROUP BY etf_code
        )
        SELECT h.stock_id, MAX(h.stock_name) AS stock_name, COUNT(DISTINCT h.etf_code) AS etf_hold_count
        FROM etf_holdings h
        INNER JOIN latest l
            ON h.etf_code = l.etf_code AND h.snapshot_date = l.snapshot_date
        WHERE h.shares > 0
        GROUP BY h.stock_id
        ORDER BY etf_hold_count DESC, h.stock_id
    """
    rows = conn.execute(sql, etf_codes).fetchall()
    return [
        {
            "stock_id": row["stock_id"],
            "stock_name": row["stock_name"] or "",
            "etf_hold_count": int(row["etf_hold_count"]),
        }
        for row in rows
    ]


def upsert_intraday_1m_bars(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO intraday_1m_bars (
            symbol, ts, open_1m, high_1m, low_1m, close_1m, volume_1m,
            cum_volume, vwap_day, day_return, rel_volume_est, ret_vs_index_day,
            order_imbalance_1, position_in_day_range, breakout_flag, source, synced_at
        ) VALUES (
            :symbol, :ts, :open_1m, :high_1m, :low_1m, :close_1m, :volume_1m,
            :cum_volume, :vwap_day, :day_return, :rel_volume_est, :ret_vs_index_day,
            :order_imbalance_1, :position_in_day_range, :breakout_flag, :source, :synced_at
        )
        ON CONFLICT(symbol, ts, source) DO UPDATE SET
            open_1m=excluded.open_1m, high_1m=excluded.high_1m, low_1m=excluded.low_1m,
            close_1m=excluded.close_1m, volume_1m=excluded.volume_1m,
            cum_volume=excluded.cum_volume, vwap_day=excluded.vwap_day,
            day_return=excluded.day_return, rel_volume_est=excluded.rel_volume_est,
            ret_vs_index_day=excluded.ret_vs_index_day,
            order_imbalance_1=excluded.order_imbalance_1,
            position_in_day_range=excluded.position_in_day_range,
            breakout_flag=excluded.breakout_flag,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_intraday_signals(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO intraday_signals (
            symbol, ts, buy_signal, etf_hold_count, rel_volume_est, ret_vs_index_day,
            position_in_day_range, order_imbalance_1, breakout_flag, reason, source, synced_at
        ) VALUES (
            :symbol, :ts, :buy_signal, :etf_hold_count, :rel_volume_est, :ret_vs_index_day,
            :position_in_day_range, :order_imbalance_1, :breakout_flag, :reason, :source, :synced_at
        )
        ON CONFLICT(symbol, ts, source) DO UPDATE SET
            buy_signal=excluded.buy_signal,
            etf_hold_count=excluded.etf_hold_count,
            rel_volume_est=excluded.rel_volume_est,
            ret_vs_index_day=excluded.ret_vs_index_day,
            position_in_day_range=excluded.position_in_day_range,
            order_imbalance_1=excluded.order_imbalance_1,
            breakout_flag=excluded.breakout_flag,
            reason=excluded.reason,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)
