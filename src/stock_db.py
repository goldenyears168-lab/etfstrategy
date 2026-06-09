"""SQLite storage for ETF daily sync (Phase 0 local).

Tables:
  daily_bars                  — TEJ ETF/index OHLCV (4 ETFs + IX0001 + IR0002)
  etf_daily_signal_snapshot   — FinMind close + 三大法人
  etf_holdings / meta         — EZMoney (統一 2 檔) + KGIFund (凱基 009816/00407A)
  stock_beta                  — 上市櫃 Beta vs ^TWII（sync_stock_beta.py，小數 2 位）
  tech_risk_daily_snapshot    — 科技風險三層（TSM ADR / SOX / 台指期 gap，sync_tech_risk_context.py）
  morning_risk_snapshot       — 早盤即時 TX/TE gap（sync_morning_futures.py · 08:30 執行雷達）
  intraday_1m_bars            — 盤中 1 分 K 與特徵（intraday_monitor.py）
  intraday_signals            — 每分鐘 buy_signal 快照
  stock_daily_bars            — 成分股日 OHLCV（FinMind，sync_stock_market_daily.py）
  stock_institutional_daily   — 成分股三大法人日淨買賣（同上）
  investment_scores           — 五維子分 + Investment Score + 觀察名單（score_engine.py）
  pm_watchlist                — 盤前觀察名單（pm_watchlist.py · 收盤寫入早盤只讀）
  portfolio_weights           — 部位配置 Position/Risk/Weight（portfolio_engine.py）
  stock_fundamental           — L8 截面（sync_fundamentals.py）
  stock_consensus             — L8.5 共識基準（sync_fundamentals.py）
  stock_financial_history     — 季/月序列（sync_fundamentals.py）
  catalyst_events             — L7 結構化催化（catalyst_engine.py · manual；sync_catalyst_news.py · perplexity）
  research_memos              — L9 備忘錄正文（investment_memo.py）
  flow_events                 — ETF Flow 事件快照（sync_flow_events.py · ② intent 後寫入）
  order_intents               — E0 待核准訂單草稿（order_intent_engine.py）
  portfolio_books             — 多帳本（Lily / Jack / Annie 等）
  portfolio_positions         — 各帳本實際持倉（stock 或 etf）
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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

CREATE TABLE IF NOT EXISTS morning_risk_snapshot (
    trade_date TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    tw_spot_date TEXT,
    tw_spot_code TEXT NOT NULL DEFAULT 'IX0001',
    tw_spot_prev_close REAL,
    tx_snapshot_id TEXT,
    tx_price REAL,
    tx_contract_date TEXT,
    tx_gap_live_pct REAL,
    te_snapshot_id TEXT,
    te_price REAL,
    te_contract_date TEXT,
    te_gap_live_pct REAL,
    te_minus_tx_pct REAL,
    source TEXT NOT NULL DEFAULT 'finmind_snapshot',
    notes TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (trade_date)
);

CREATE INDEX IF NOT EXISTS idx_morning_risk_trade_date
    ON morning_risk_snapshot (trade_date DESC);

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

CREATE TABLE IF NOT EXISTS stock_daily_bars (
    stock_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL NOT NULL,
    volume INTEGER,
    source TEXT NOT NULL DEFAULT 'finmind',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, trade_date, source)
);

CREATE INDEX IF NOT EXISTS idx_stock_daily_bars_date
    ON stock_daily_bars (trade_date, stock_id);

CREATE TABLE IF NOT EXISTS stock_institutional_daily (
    stock_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close_price REAL,
    foreign_net REAL,
    investment_trust_net REAL,
    dealer_self_net REAL,
    three_institution_net REAL,
    source TEXT NOT NULL DEFAULT 'finmind',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, trade_date, source)
);

CREATE INDEX IF NOT EXISTS idx_stock_institutional_date
    ON stock_institutional_daily (trade_date, stock_id);

CREATE TABLE IF NOT EXISTS investment_scores (
    stock_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    score_version TEXT NOT NULL,
    stock_name TEXT,
    smart_money REAL NOT NULL,
    catalyst REAL NOT NULL,
    expectation REAL NOT NULL,
    fundamental REAL NOT NULL,
    risk REAL NOT NULL,
    investment_score REAL NOT NULL,
    watchlist TEXT NOT NULL,
    pool_reason TEXT,
    money_rank INTEGER,
    event_rank INTEGER,
    position_intent TEXT,
    tech_risk_flag TEXT,
    metadata_json TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, as_of_date, score_version)
);

CREATE INDEX IF NOT EXISTS idx_investment_scores_date
    ON investment_scores (as_of_date, watchlist, investment_score DESC);

CREATE TABLE IF NOT EXISTS pm_watchlist (
    stock_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    score_version TEXT NOT NULL,
    stock_name TEXT,
    investment_score REAL NOT NULL,
    watchlist TEXT NOT NULL,
    entry_signal TEXT NOT NULL,
    entry_tags_json TEXT,
    chip_tag TEXT,
    pm_bucket TEXT NOT NULL,
    flow_score REAL,
    chip_score REAL,
    tech_score REAL,
    catalyst_score REAL,
    fundamental_score REAL,
    note TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, as_of_date, score_version)
);

CREATE INDEX IF NOT EXISTS idx_pm_watchlist_date
    ON pm_watchlist (as_of_date, pm_bucket, investment_score DESC);

CREATE TABLE IF NOT EXISTS portfolio_weights (
    stock_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    score_version TEXT NOT NULL,
    stock_name TEXT,
    watchlist TEXT NOT NULL,
    position_score REAL NOT NULL,
    risk_score REAL NOT NULL,
    portfolio_weight_pct REAL NOT NULL,
    suggested_ntd REAL NOT NULL,
    capital_ntd REAL,
    entry_signal TEXT,
    entry_tags_json TEXT,
    pm_bucket TEXT,
    note TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, as_of_date, score_version)
);

CREATE INDEX IF NOT EXISTS idx_portfolio_weights_date
    ON portfolio_weights (as_of_date, portfolio_weight_pct DESC);

CREATE TABLE IF NOT EXISTS stock_fundamental (
    stock_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    pe REAL,
    pb REAL,
    roe_ttm REAL,
    eps_ttm REAL,
    eps_latest_q REAL,
    roe_latest_q REAL,
    dividend_yield REAL,
    revenue_yoy_pct REAL,
    revenue_mom_accel_pp REAL,
    source TEXT NOT NULL DEFAULT 'finmind',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, as_of_date, source)
);

CREATE TABLE IF NOT EXISTS stock_consensus (
    stock_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    metric TEXT NOT NULL,
    consensus_value REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'finmind',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, as_of_date, metric, source)
);

CREATE TABLE IF NOT EXISTS stock_financial_history (
    stock_id TEXT NOT NULL,
    period_date TEXT NOT NULL,
    period_type TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'finmind',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, period_date, period_type, metric, source)
);

CREATE INDEX IF NOT EXISTS idx_stock_financial_history_stock
    ON stock_financial_history (stock_id, metric, period_date DESC);

CREATE TABLE IF NOT EXISTS catalyst_events (
    event_id TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    event_date TEXT NOT NULL,
    catalyst_type TEXT NOT NULL,
    headline TEXT NOT NULL,
    polarity TEXT NOT NULL DEFAULT 'NEUTRAL',
    explains_etf_add TEXT NOT NULL DEFAULT 'NONE',
    confidence INTEGER NOT NULL DEFAULT 50,
    sources_json TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (event_id)
);

CREATE INDEX IF NOT EXISTS idx_catalyst_events_stock_date
    ON catalyst_events (stock_id, event_date DESC);

CREATE TABLE IF NOT EXISTS research_memos (
    memo_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    watchlist TEXT NOT NULL,
    investment_score REAL,
    body_md TEXT NOT NULL,
    context_json TEXT,
    llm_used INTEGER NOT NULL DEFAULT 0,
    audit_passed INTEGER NOT NULL DEFAULT 1,
    audit_notes TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (memo_date, stock_id)
);

CREATE TABLE IF NOT EXISTS flow_events (
    event_date TEXT NOT NULL,
    prev_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT,
    net_side TEXT NOT NULL,
    consensus TEXT NOT NULL,
    intent TEXT NOT NULL,
    conviction REAL NOT NULL,
    implied_flow_ntd REAL,
    etf_count INTEGER NOT NULL,
    source_etfs TEXT NOT NULL,
    flow_version TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (event_date, stock_id, flow_version)
);

CREATE INDEX IF NOT EXISTS idx_flow_events_date
    ON flow_events (event_date DESC, net_side);

CREATE TABLE IF NOT EXISTS order_intents (
    stock_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    intent_version TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    score_version TEXT NOT NULL,
    stock_name TEXT,
    side TEXT NOT NULL DEFAULT 'BUY',
    ref_price REAL NOT NULL,
    limit_price REAL NOT NULL,
    qty INTEGER NOT NULL,
    suggested_ntd REAL NOT NULL,
    pm_bucket TEXT NOT NULL,
    entry_signal TEXT NOT NULL,
    entry_tags_json TEXT,
    benchmark_type TEXT,
    benchmark_price REAL,
    stop_price REAL,
    target_price REAL,
    order_type_planned TEXT NOT NULL DEFAULT 'pending_open',
    open_price REAL,
    order_type_effective TEXT,
    status TEXT NOT NULL,
    block_reason TEXT,
    ips_version TEXT,
    chip_tag TEXT,
    investment_score REAL,
    evaluation_mode TEXT,
    price_source TEXT,
    price_snapshot REAL,
    price_snapshot_json TEXT,
    size_scale REAL,
    eval_run_id TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, trade_date, intent_version)
);

CREATE INDEX IF NOT EXISTS idx_order_intents_trade
    ON order_intents (trade_date DESC, status);

CREATE TABLE IF NOT EXISTS execution_eval_runs (
    eval_run_id TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    evaluation_mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ingest_ran INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT,
    report_path TEXT
);

CREATE TABLE IF NOT EXISTS portfolio_books (
    book_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    book_type TEXT NOT NULL DEFAULT 'discretionary',
    etf_codes_json TEXT,
    notes TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_positions (
    book_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL DEFAULT 'stock',
    stock_name TEXT,
    shares REAL,
    cost_basis REAL,
    entry_date TEXT,
    market_value REAL,
    weight_pct REAL,
    notes TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (book_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_portfolio_positions_book
    ON portfolio_positions (book_id);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """既有 DB 補欄位（CREATE IF NOT EXISTS 不會自動 ALTER）。"""
    migrations: list[tuple[str, str, str]] = [
        (
            "stock_fundamental",
            "eps_latest_q",
            "ALTER TABLE stock_fundamental ADD COLUMN eps_latest_q REAL",
        ),
        (
            "stock_fundamental",
            "roe_latest_q",
            "ALTER TABLE stock_fundamental ADD COLUMN roe_latest_q REAL",
        ),
        (
            "pm_watchlist",
            "entry_tags_json",
            "ALTER TABLE pm_watchlist ADD COLUMN entry_tags_json TEXT",
        ),
        (
            "order_intents",
            "evaluation_mode",
            "ALTER TABLE order_intents ADD COLUMN evaluation_mode TEXT",
        ),
        (
            "order_intents",
            "price_source",
            "ALTER TABLE order_intents ADD COLUMN price_source TEXT",
        ),
        (
            "order_intents",
            "eval_run_id",
            "ALTER TABLE order_intents ADD COLUMN eval_run_id TEXT",
        ),
        (
            "order_intents",
            "price_snapshot",
            "ALTER TABLE order_intents ADD COLUMN price_snapshot REAL",
        ),
        (
            "order_intents",
            "price_snapshot_json",
            "ALTER TABLE order_intents ADD COLUMN price_snapshot_json TEXT",
        ),
        (
            "order_intents",
            "size_scale",
            "ALTER TABLE order_intents ADD COLUMN size_scale REAL",
        ),
    ]
    for table, col, ddl in migrations:
        try:
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.OperationalError:
            continue
        if cols and col not in cols:
            conn.execute(ddl)
    conn.commit()


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate_schema(conn)
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


def load_stock_market_map(
    conn: sqlite3.Connection,
    stock_ids: list[str] | None = None,
) -> dict[str, str]:
    """Latest stock_beta.market per stock_id（TSE|OTC）。"""
    try:
        as_of_row = conn.execute(
            "SELECT MAX(as_of_date) AS d FROM stock_beta"
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if as_of_row is None or as_of_row["d"] is None:
        return {}
    as_of_date = as_of_row["d"]
    if stock_ids:
        placeholders = ",".join("?" * len(stock_ids))
        rows = conn.execute(
            f"""
            SELECT stock_id, market FROM stock_beta
            WHERE as_of_date = ? AND stock_id IN ({placeholders})
            """,
            (as_of_date, *stock_ids),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT stock_id, market FROM stock_beta WHERE as_of_date = ?",
            (as_of_date,),
        ).fetchall()
    return {row["stock_id"]: row["market"] for row in rows}


def upsert_stock_fundamental(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_fundamental (
            stock_id, as_of_date, pe, pb, roe_ttm, eps_ttm,
            eps_latest_q, roe_latest_q, dividend_yield,
            revenue_yoy_pct, revenue_mom_accel_pp, source, synced_at
        ) VALUES (
            :stock_id, :as_of_date, :pe, :pb, :roe_ttm, :eps_ttm,
            :eps_latest_q, :roe_latest_q, :dividend_yield,
            :revenue_yoy_pct, :revenue_mom_accel_pp, :source, :synced_at
        )
        ON CONFLICT(stock_id, as_of_date, source) DO UPDATE SET
            pe=excluded.pe, pb=excluded.pb, roe_ttm=excluded.roe_ttm,
            eps_ttm=excluded.eps_ttm,
            eps_latest_q=excluded.eps_latest_q, roe_latest_q=excluded.roe_latest_q,
            dividend_yield=excluded.dividend_yield,
            revenue_yoy_pct=excluded.revenue_yoy_pct,
            revenue_mom_accel_pp=excluded.revenue_mom_accel_pp,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_consensus(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_consensus (
            stock_id, as_of_date, metric, consensus_value, source, synced_at
        ) VALUES (
            :stock_id, :as_of_date, :metric, :consensus_value, :source, :synced_at
        )
        ON CONFLICT(stock_id, as_of_date, metric, source) DO UPDATE SET
            consensus_value=excluded.consensus_value,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_financial_history(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_financial_history (
            stock_id, period_date, period_type, metric, value, source, synced_at
        ) VALUES (
            :stock_id, :period_date, :period_type, :metric, :value, :source, :synced_at
        )
        ON CONFLICT(stock_id, period_date, period_type, metric, source) DO UPDATE SET
            value=excluded.value,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_latest_fundamental_map(
    conn: sqlite3.Connection,
    stock_ids: list[str] | None = None,
) -> dict[str, sqlite3.Row]:
    try:
        if stock_ids:
            placeholders = ",".join("?" * len(stock_ids))
            rows = conn.execute(
                f"""
                SELECT f.* FROM stock_fundamental f
                INNER JOIN (
                    SELECT stock_id, MAX(as_of_date) AS d
                    FROM stock_fundamental
                    WHERE stock_id IN ({placeholders})
                    GROUP BY stock_id
                ) latest ON f.stock_id = latest.stock_id AND f.as_of_date = latest.d
                """,
                stock_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT f.* FROM stock_fundamental f
                INNER JOIN (
                    SELECT stock_id, MAX(as_of_date) AS d
                    FROM stock_fundamental
                    GROUP BY stock_id
                ) latest ON f.stock_id = latest.stock_id AND f.as_of_date = latest.d
                """
            ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["stock_id"]: row for row in rows}


def load_latest_consensus_map(
    conn: sqlite3.Connection,
    stock_ids: list[str] | None = None,
) -> dict[str, dict[str, float]]:
    """stock_id -> {metric: consensus_value}（各 metric 取最新 as_of_date）。"""
    try:
        if stock_ids:
            placeholders = ",".join("?" * len(stock_ids))
            rows = conn.execute(
                f"""
                SELECT c.stock_id, c.metric, c.consensus_value
                FROM stock_consensus c
                INNER JOIN (
                    SELECT stock_id, metric, MAX(as_of_date) AS d
                    FROM stock_consensus
                    WHERE stock_id IN ({placeholders})
                    GROUP BY stock_id, metric
                ) latest
                    ON c.stock_id = latest.stock_id
                    AND c.metric = latest.metric
                    AND c.as_of_date = latest.d
                """,
                stock_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT c.stock_id, c.metric, c.consensus_value
                FROM stock_consensus c
                INNER JOIN (
                    SELECT stock_id, metric, MAX(as_of_date) AS d
                    FROM stock_consensus
                    GROUP BY stock_id, metric
                ) latest
                    ON c.stock_id = latest.stock_id
                    AND c.metric = latest.metric
                    AND c.as_of_date = latest.d
                """
            ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        out.setdefault(row["stock_id"], {})[row["metric"]] = float(row["consensus_value"])
    return out


def upsert_catalyst_events(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    ingested_at = utc_now_iso()
    sql = """
        INSERT INTO catalyst_events (
            event_id, stock_id, event_date, catalyst_type, headline,
            polarity, explains_etf_add, confidence, sources_json,
            source, ingested_at
        ) VALUES (
            :event_id, :stock_id, :event_date, :catalyst_type, :headline,
            :polarity, :explains_etf_add, :confidence, :sources_json,
            :source, :ingested_at
        )
        ON CONFLICT(event_id) DO UPDATE SET
            polarity=excluded.polarity,
            explains_etf_add=excluded.explains_etf_add,
            confidence=excluded.confidence,
            sources_json=excluded.sources_json,
            source=excluded.source,
            ingested_at=excluded.ingested_at
    """
    payload = [{**r, "ingested_at": ingested_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_catalyst_events(
    conn: sqlite3.Connection,
    *,
    stock_ids: list[str] | None = None,
    window_days: int = 7,
    as_of: str | None = None,
) -> list[sqlite3.Row]:
    try:
        ref = as_of or date.today().isoformat()
        start = (
            datetime.fromisoformat(ref).date()
            - timedelta(days=window_days)
        ).isoformat()
        if stock_ids:
            placeholders = ",".join("?" * len(stock_ids))
            return conn.execute(
                f"""
                SELECT * FROM catalyst_events
                WHERE event_date >= ? AND event_date <= ?
                  AND stock_id IN ({placeholders})
                ORDER BY event_date DESC, confidence DESC
                """,
                [start, ref, *stock_ids],
            ).fetchall()
        return conn.execute(
            """
            SELECT * FROM catalyst_events
            WHERE event_date >= ? AND event_date <= ?
            ORDER BY event_date DESC, confidence DESC
            """,
            (start, ref),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def load_memo_candidates(
    conn: sqlite3.Connection,
    *,
    as_of_date: str | None = None,
    top_n: int = 10,
    watchlist: str = "首要觀察",
) -> list[sqlite3.Row]:
    """觀察名單首要觀察 TopN；若無則改取同 as_of 綜合評分最高（供備忘草稿）。"""
    try:
        if as_of_date is None:
            row = conn.execute(
                "SELECT MAX(as_of_date) AS d FROM investment_scores"
            ).fetchone()
            if row is None or row["d"] is None:
                return []
            as_of_date = row["d"]
        rows = conn.execute(
            """
            SELECT * FROM investment_scores
            WHERE as_of_date = ? AND watchlist IN (?, 'A')
            ORDER BY investment_score DESC
            LIMIT ?
            """,
            (as_of_date, watchlist, top_n),
        ).fetchall()
        if rows:
            return rows
        return conn.execute(
            """
            SELECT * FROM investment_scores
            WHERE as_of_date = ?
            ORDER BY investment_score DESC
            LIMIT ?
            """,
            (as_of_date, top_n),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def upsert_research_memos(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO research_memos (
            memo_date, stock_id, rank, watchlist, investment_score,
            body_md, context_json, llm_used, audit_passed, audit_notes, synced_at
        ) VALUES (
            :memo_date, :stock_id, :rank, :watchlist, :investment_score,
            :body_md, :context_json, :llm_used, :audit_passed, :audit_notes, :synced_at
        )
        ON CONFLICT(memo_date, stock_id) DO UPDATE SET
            rank=excluded.rank,
            watchlist=excluded.watchlist,
            investment_score=excluded.investment_score,
            body_md=excluded.body_md,
            context_json=excluded.context_json,
            llm_used=excluded.llm_used,
            audit_passed=excluded.audit_passed,
            audit_notes=excluded.audit_notes,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_tsm_adr_spread_before(
    conn: sqlite3.Connection,
    trade_date: str,
) -> tuple[str | None, float | None]:
    """trade_date 開盤前一夜 TSM ADR 日報酬（daily_bars.spread）。"""
    try:
        row = conn.execute(
            """
            SELECT date, spread
            FROM daily_bars
            WHERE code = 'TSM_ADR' AND date < ? AND spread IS NOT NULL
            ORDER BY date DESC
            LIMIT 1
            """,
            (trade_date,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None, None
    if row is None:
        return None, None
    return row[0], float(row[1])


def upsert_morning_risk_snapshot(conn: sqlite3.Connection, row: dict) -> int:
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO morning_risk_snapshot (
            trade_date, captured_at, tw_spot_date, tw_spot_code, tw_spot_prev_close,
            tx_snapshot_id, tx_price, tx_contract_date, tx_gap_live_pct,
            te_snapshot_id, te_price, te_contract_date, te_gap_live_pct,
            te_minus_tx_pct, source, notes, synced_at
        ) VALUES (
            :trade_date, :captured_at, :tw_spot_date, :tw_spot_code, :tw_spot_prev_close,
            :tx_snapshot_id, :tx_price, :tx_contract_date, :tx_gap_live_pct,
            :te_snapshot_id, :te_price, :te_contract_date, :te_gap_live_pct,
            :te_minus_tx_pct, :source, :notes, :synced_at
        )
        ON CONFLICT(trade_date) DO UPDATE SET
            captured_at=excluded.captured_at,
            tw_spot_date=excluded.tw_spot_date,
            tw_spot_code=excluded.tw_spot_code,
            tw_spot_prev_close=excluded.tw_spot_prev_close,
            tx_snapshot_id=excluded.tx_snapshot_id,
            tx_price=excluded.tx_price,
            tx_contract_date=excluded.tx_contract_date,
            tx_gap_live_pct=excluded.tx_gap_live_pct,
            te_snapshot_id=excluded.te_snapshot_id,
            te_price=excluded.te_price,
            te_contract_date=excluded.te_contract_date,
            te_gap_live_pct=excluded.te_gap_live_pct,
            te_minus_tx_pct=excluded.te_minus_tx_pct,
            source=excluded.source,
            notes=excluded.notes,
            synced_at=excluded.synced_at
    """
    payload = {**row, "synced_at": synced_at}
    conn.execute(sql, payload)
    conn.commit()
    return 1


def load_latest_morning_risk(
    conn: sqlite3.Connection,
    trade_date: str | None = None,
) -> sqlite3.Row | None:
    """morning_risk_snapshot；08:30 即時 TX/TE gap。"""
    try:
        if trade_date:
            row = conn.execute(
                """
                SELECT trade_date, captured_at, tw_spot_date, tw_spot_code,
                       tw_spot_prev_close, tx_snapshot_id, tx_price, tx_contract_date,
                       tx_gap_live_pct, te_snapshot_id, te_price, te_contract_date,
                       te_gap_live_pct, te_minus_tx_pct, source, notes
                FROM morning_risk_snapshot
                WHERE trade_date = ?
                """,
                (trade_date,),
            ).fetchone()
            if row is not None:
                return row
        return conn.execute(
            """
            SELECT trade_date, captured_at, tw_spot_date, tw_spot_code,
                   tw_spot_prev_close, tx_snapshot_id, tx_price, tx_contract_date,
                   tx_gap_live_pct, te_snapshot_id, te_price, te_contract_date,
                   te_gap_live_pct, te_minus_tx_pct, source, notes
            FROM morning_risk_snapshot
            ORDER BY trade_date DESC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def load_execution_tx_gap(
    conn: sqlite3.Connection,
    trade_date: str | None = None,
) -> tuple[float | None, str]:
    """執行層台指 gap：優先 morning 即時，fallback tech_risk 隔夜。"""
    morning = load_latest_morning_risk(conn, trade_date=trade_date)
    if morning is not None and morning["tx_gap_live_pct"] is not None:
        return float(morning["tx_gap_live_pct"]), "morning_live"
    tech = load_latest_tech_risk(conn, trade_date=trade_date)
    if tech is not None and tech["tx_gap_pct"] is not None:
        return float(tech["tx_gap_pct"]), "tech_risk_overnight"
    return None, "none"


def load_latest_tech_risk(
    conn: sqlite3.Connection,
    trade_date: str | None = None,
) -> sqlite3.Row | None:
    """tech_risk_daily_snapshot；表空或不存在則 None。

    trade_date 有值時優先取 session_date 吻合列（開盤前對應昨夜美股），
    否則回傳最新一列。
    """
    try:
        if trade_date:
            row = conn.execute(
                """
                SELECT session_date, us_trade_date, tsm_daily_return_pct,
                       sox_daily_return_pct, tx_gap_pct, te_overnight_pct
                FROM tech_risk_daily_snapshot
                WHERE session_date = ?
                """,
                (trade_date,),
            ).fetchone()
            if row is not None:
                return row
        return conn.execute(
            """
            SELECT session_date, us_trade_date, tsm_daily_return_pct,
                   sox_daily_return_pct, tx_gap_pct, te_overnight_pct
            FROM tech_risk_daily_snapshot
            ORDER BY session_date DESC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def upsert_investment_scores(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO investment_scores (
            stock_id, as_of_date, score_version, stock_name,
            smart_money, catalyst, expectation, fundamental, risk,
            investment_score, watchlist,
            pool_reason, money_rank, event_rank, position_intent,
            tech_risk_flag, metadata_json, synced_at
        ) VALUES (
            :stock_id, :as_of_date, :score_version, :stock_name,
            :smart_money, :catalyst, :expectation, :fundamental, :risk,
            :investment_score, :watchlist,
            :pool_reason, :money_rank, :event_rank, :position_intent,
            :tech_risk_flag, :metadata_json, :synced_at
        )
        ON CONFLICT(stock_id, as_of_date, score_version) DO UPDATE SET
            stock_name=excluded.stock_name,
            smart_money=excluded.smart_money,
            catalyst=excluded.catalyst,
            expectation=excluded.expectation,
            fundamental=excluded.fundamental,
            risk=excluded.risk,
            investment_score=excluded.investment_score,
            watchlist=excluded.watchlist,
            pool_reason=excluded.pool_reason,
            money_rank=excluded.money_rank,
            event_rank=excluded.event_rank,
            position_intent=excluded.position_intent,
            tech_risk_flag=excluded.tech_risk_flag,
            metadata_json=excluded.metadata_json,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_pm_watchlist(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO pm_watchlist (
            stock_id, as_of_date, score_version, stock_name,
            investment_score, watchlist, entry_signal, entry_tags_json, chip_tag,
            pm_bucket,
            flow_score, chip_score, tech_score, catalyst_score, fundamental_score,
            note, synced_at
        ) VALUES (
            :stock_id, :as_of_date, :score_version, :stock_name,
            :investment_score, :watchlist, :entry_signal, :entry_tags_json, :chip_tag,
            :pm_bucket,
            :flow_score, :chip_score, :tech_score, :catalyst_score, :fundamental_score,
            :note, :synced_at
        )
        ON CONFLICT(stock_id, as_of_date, score_version) DO UPDATE SET
            stock_name=excluded.stock_name,
            investment_score=excluded.investment_score,
            watchlist=excluded.watchlist,
            entry_signal=excluded.entry_signal,
            entry_tags_json=excluded.entry_tags_json,
            chip_tag=excluded.chip_tag,
            pm_bucket=excluded.pm_bucket,
            flow_score=excluded.flow_score,
            chip_score=excluded.chip_score,
            tech_score=excluded.tech_score,
            catalyst_score=excluded.catalyst_score,
            fundamental_score=excluded.fundamental_score,
            note=excluded.note,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_latest_pm_watchlist(
    conn: sqlite3.Connection,
    *,
    score_version: str | None = None,
) -> list[sqlite3.Row]:
    version = score_version or "p4-v2"
    try:
        row = conn.execute(
            """
            SELECT MAX(as_of_date) AS d
            FROM pm_watchlist
            WHERE score_version = ?
            """,
            (version,),
        ).fetchone()
    except sqlite3.OperationalError:
        return []
    if row is None or row["d"] is None:
        return []
    return conn.execute(
        """
        SELECT *
        FROM pm_watchlist
        WHERE as_of_date = ? AND score_version = ?
        ORDER BY
            CASE pm_bucket
                WHEN '突破' THEN 0
                WHEN '觀察' THEN 1
                WHEN 'BREAKOUT' THEN 0
                WHEN 'RESEARCH' THEN 1
                ELSE 2
            END,
            investment_score DESC,
            stock_id
        """,
        (row["d"], version),
    ).fetchall()


def upsert_portfolio_weights(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO portfolio_weights (
            stock_id, as_of_date, score_version, stock_name, watchlist,
            position_score, risk_score, portfolio_weight_pct, suggested_ntd,
            capital_ntd, entry_signal, entry_tags_json, pm_bucket, note, synced_at
        ) VALUES (
            :stock_id, :as_of_date, :score_version, :stock_name, :watchlist,
            :position_score, :risk_score, :portfolio_weight_pct, :suggested_ntd,
            :capital_ntd, :entry_signal, :entry_tags_json, :pm_bucket, :note, :synced_at
        )
        ON CONFLICT(stock_id, as_of_date, score_version) DO UPDATE SET
            stock_name=excluded.stock_name,
            watchlist=excluded.watchlist,
            position_score=excluded.position_score,
            risk_score=excluded.risk_score,
            portfolio_weight_pct=excluded.portfolio_weight_pct,
            suggested_ntd=excluded.suggested_ntd,
            capital_ntd=excluded.capital_ntd,
            entry_signal=excluded.entry_signal,
            entry_tags_json=excluded.entry_tags_json,
            pm_bucket=excluded.pm_bucket,
            note=excluded.note,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_latest_portfolio_weights(
    conn: sqlite3.Connection,
    *,
    score_version: str | None = None,
) -> list[sqlite3.Row]:
    version = score_version or "p4-v2"
    try:
        row = conn.execute(
            """
            SELECT MAX(as_of_date) AS d
            FROM portfolio_weights
            WHERE score_version = ?
            """,
            (version,),
        ).fetchone()
    except sqlite3.OperationalError:
        return []
    if row is None or row["d"] is None:
        return []
    return conn.execute(
        """
        SELECT *
        FROM portfolio_weights
        WHERE as_of_date = ? AND score_version = ?
        ORDER BY portfolio_weight_pct DESC, position_score DESC, stock_id
        """,
        (row["d"], version),
    ).fetchall()


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


def upsert_stock_daily_bars(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_daily_bars (
            stock_id, trade_date, open, high, low, close, volume, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :open, :high, :low, :close, :volume, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_institutional_daily(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_institutional_daily (
            stock_id, trade_date, close_price,
            foreign_net, investment_trust_net, dealer_self_net, three_institution_net,
            source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :close_price,
            :foreign_net, :investment_trust_net, :dealer_self_net, :three_institution_net,
            :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
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


@dataclass(frozen=True)
class StockMarketCoverage:
    stock_id: str
    bar_max: str | None
    bar_count_window: int
    inst_max: str | None
    inst_count_window: int


def load_stock_market_coverage_map(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    window_start: str,
    window_end: str,
    source: str = "finmind",
) -> dict[str, StockMarketCoverage]:
    """批次查各股在回溯窗內的 K 線/法人覆蓋（用於跳過已同步）。"""
    if not stock_ids:
        return {}
    placeholders = ",".join("?" * len(stock_ids))
    params = [source, window_start, window_end, *stock_ids]
    bar_rows = conn.execute(
        f"""
        SELECT stock_id, MAX(trade_date) AS bar_max, COUNT(*) AS n
        FROM stock_daily_bars
        WHERE source = ? AND trade_date >= ? AND trade_date <= ?
          AND stock_id IN ({placeholders})
        GROUP BY stock_id
        """,
        params,
    ).fetchall()
    inst_rows = conn.execute(
        f"""
        SELECT stock_id, MAX(trade_date) AS inst_max, COUNT(*) AS n
        FROM stock_institutional_daily
        WHERE source = ? AND trade_date >= ? AND trade_date <= ?
          AND stock_id IN ({placeholders})
        GROUP BY stock_id
        """,
        params,
    ).fetchall()
    bar_by_id = {r["stock_id"]: (r["bar_max"], int(r["n"])) for r in bar_rows}
    inst_by_id = {r["stock_id"]: (r["inst_max"], int(r["n"])) for r in inst_rows}
    out: dict[str, StockMarketCoverage] = {}
    for sid in stock_ids:
        b = bar_by_id.get(sid, (None, 0))
        i = inst_by_id.get(sid, (None, 0))
        out[sid] = StockMarketCoverage(
            stock_id=sid,
            bar_max=b[0],
            bar_count_window=b[1],
            inst_max=i[0],
            inst_count_window=i[1],
        )
    return out


def count_stock_market_rows(conn: sqlite3.Connection) -> tuple[int, int, str | None, str | None]:
    """(bars 筆數, institutional 筆數, 最新 bar 日, 最新法人日)"""
    bar_n = conn.execute("SELECT COUNT(*) FROM stock_daily_bars").fetchone()[0]
    inst_n = conn.execute("SELECT COUNT(*) FROM stock_institutional_daily").fetchone()[0]
    bar_max = conn.execute("SELECT MAX(trade_date) FROM stock_daily_bars").fetchone()[0]
    inst_max = conn.execute("SELECT MAX(trade_date) FROM stock_institutional_daily").fetchone()[0]
    return int(bar_n), int(inst_n), bar_max, inst_max


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


def upsert_flow_events(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO flow_events (
            event_date, prev_date, stock_id, stock_name, net_side, consensus, intent,
            conviction, implied_flow_ntd, etf_count, source_etfs, flow_version, synced_at
        ) VALUES (
            :event_date, :prev_date, :stock_id, :stock_name, :net_side, :consensus, :intent,
            :conviction, :implied_flow_ntd, :etf_count, :source_etfs, :flow_version, :synced_at
        )
        ON CONFLICT(event_date, stock_id, flow_version) DO UPDATE SET
            prev_date=excluded.prev_date,
            stock_name=excluded.stock_name,
            net_side=excluded.net_side,
            consensus=excluded.consensus,
            intent=excluded.intent,
            conviction=excluded.conviction,
            implied_flow_ntd=excluded.implied_flow_ntd,
            etf_count=excluded.etf_count,
            source_etfs=excluded.source_etfs,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def list_flow_event_dates(
    conn: sqlite3.Connection,
    *,
    flow_version: str,
    as_of: str,
    lookback: int,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT event_date AS d
        FROM flow_events
        WHERE flow_version = ? AND event_date <= ?
        ORDER BY d DESC
        LIMIT ?
        """,
        (flow_version, as_of, lookback),
    ).fetchall()
    dates = [str(r["d"]) for r in rows]
    dates.reverse()
    return dates


def load_flow_events(
    conn: sqlite3.Connection,
    *,
    flow_version: str,
    event_dates: tuple[str, ...] | list[str],
) -> list[sqlite3.Row]:
    if not event_dates:
        return []
    placeholders = ",".join("?" * len(event_dates))
    return conn.execute(
        f"""
        SELECT * FROM flow_events
        WHERE flow_version = ? AND event_date IN ({placeholders})
        ORDER BY event_date ASC, stock_id ASC
        """,
        (flow_version, *event_dates),
    ).fetchall()


INTENT_VERSION_DEFAULT = "e0-v1"


def upsert_order_intents(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO order_intents (
            stock_id, trade_date, intent_version, as_of_date, score_version, stock_name,
            side, ref_price, limit_price, qty, suggested_ntd, pm_bucket, entry_signal,
            entry_tags_json, benchmark_type, benchmark_price, stop_price, target_price,
            order_type_planned, open_price, order_type_effective, status, block_reason,
            ips_version, chip_tag, investment_score,
            evaluation_mode, price_source, price_snapshot, price_snapshot_json, size_scale,
            eval_run_id, synced_at
        ) VALUES (
            :stock_id, :trade_date, :intent_version, :as_of_date, :score_version, :stock_name,
            :side, :ref_price, :limit_price, :qty, :suggested_ntd, :pm_bucket, :entry_signal,
            :entry_tags_json, :benchmark_type, :benchmark_price, :stop_price, :target_price,
            :order_type_planned, :open_price, :order_type_effective, :status, :block_reason,
            :ips_version, :chip_tag, :investment_score,
            :evaluation_mode, :price_source, :price_snapshot, :price_snapshot_json, :size_scale,
            :eval_run_id, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, intent_version) DO UPDATE SET
            as_of_date=excluded.as_of_date,
            score_version=excluded.score_version,
            stock_name=excluded.stock_name,
            side=excluded.side,
            ref_price=excluded.ref_price,
            limit_price=excluded.limit_price,
            qty=excluded.qty,
            suggested_ntd=excluded.suggested_ntd,
            pm_bucket=excluded.pm_bucket,
            entry_signal=excluded.entry_signal,
            entry_tags_json=excluded.entry_tags_json,
            benchmark_type=excluded.benchmark_type,
            benchmark_price=excluded.benchmark_price,
            stop_price=excluded.stop_price,
            target_price=excluded.target_price,
            order_type_planned=excluded.order_type_planned,
            open_price=excluded.open_price,
            order_type_effective=excluded.order_type_effective,
            status=excluded.status,
            block_reason=excluded.block_reason,
            ips_version=excluded.ips_version,
            chip_tag=excluded.chip_tag,
            investment_score=excluded.investment_score,
            evaluation_mode=excluded.evaluation_mode,
            price_source=excluded.price_source,
            price_snapshot=excluded.price_snapshot,
            price_snapshot_json=excluded.price_snapshot_json,
            size_scale=excluded.size_scale,
            eval_run_id=excluded.eval_run_id,
            synced_at=excluded.synced_at
    """
    defaults = {
        "evaluation_mode": None,
        "price_source": None,
        "price_snapshot": None,
        "price_snapshot_json": None,
        "size_scale": 1.0,
        "eval_run_id": None,
    }
    payload = [{**defaults, **r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_order_intents(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    intent_version: str = INTENT_VERSION_DEFAULT,
) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            """
            SELECT * FROM order_intents
            WHERE trade_date = ? AND intent_version = ?
            ORDER BY investment_score DESC, stock_id
            """,
            (trade_date, intent_version),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def count_approved_order_intents(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    intent_version: str = INTENT_VERSION_DEFAULT,
) -> int:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM order_intents
            WHERE trade_date = ? AND intent_version = ? AND status = 'approved'
            """,
            (trade_date, intent_version),
        ).fetchone()
        return int(row["c"]) if row else 0
    except sqlite3.OperationalError:
        return 0


def demote_approved_order_intents(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    intent_version: str = INTENT_VERSION_DEFAULT,
) -> int:
    synced_at = utc_now_iso()
    cur = conn.execute(
        """
        UPDATE order_intents
        SET status = 'draft', synced_at = ?
        WHERE trade_date = ? AND intent_version = ? AND status = 'approved'
        """,
        (synced_at, trade_date, intent_version),
    )
    conn.commit()
    return cur.rowcount


def insert_execution_eval_run(
    conn: sqlite3.Connection,
    *,
    eval_run_id: str,
    trade_date: str,
    evaluation_mode: str,
    ingest_ran: bool,
    summary_json: str | None,
    report_path: str | None,
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO execution_eval_runs (
                eval_run_id, trade_date, evaluation_mode, created_at,
                ingest_ran, summary_json, report_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                eval_run_id,
                trade_date,
                evaluation_mode,
                utc_now_iso(),
                1 if ingest_ran else 0,
                summary_json,
                report_path,
            ),
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass


def approve_order_intents(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    intent_version: str = INTENT_VERSION_DEFAULT,
) -> int:
    synced_at = utc_now_iso()
    cur = conn.execute(
        """
        UPDATE order_intents
        SET status = 'approved', synced_at = ?
        WHERE trade_date = ? AND intent_version = ?
          AND status = 'draft' AND (block_reason IS NULL OR block_reason = '')
        """,
        (synced_at, trade_date, intent_version),
    )
    conn.commit()
    return cur.rowcount


def apply_open_prices_to_intents(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    open_by_stock: dict[str, float],
    intent_version: str = INTENT_VERSION_DEFAULT,
) -> int:
    """E0：寫入開盤價與 order_type_effective（approved 列）。"""
    from open_execution_policy import ORDER_PENDING_OPEN, resolve_open_execution
    from investment_policy import load_investment_policy

    ips = load_investment_policy()
    rows = load_order_intents(conn, trade_date=trade_date, intent_version=intent_version)
    n = 0
    synced_at = utc_now_iso()
    for row in rows:
        if row["status"] != "approved":
            continue
        sid = row["stock_id"]
        if sid not in open_by_stock:
            continue
        op = float(open_by_stock[sid])
        ref = float(row["ref_price"])
        decision = resolve_open_execution(ref_price=ref, open_price=op, ips=ips)
        conn.execute(
            """
            UPDATE order_intents
            SET open_price = ?, order_type_effective = ?, synced_at = ?
            WHERE stock_id = ? AND trade_date = ? AND intent_version = ?
            """,
            (
                op,
                decision.order_type_effective,
                synced_at,
                sid,
                trade_date,
                intent_version,
            ),
        )
        n += 1
    conn.commit()
    return n


def upsert_portfolio_books(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO portfolio_books (
            book_id, display_name, book_type, etf_codes_json, notes,
            is_active, synced_at
        ) VALUES (
            :book_id, :display_name, :book_type, :etf_codes_json, :notes,
            :is_active, :synced_at
        )
        ON CONFLICT(book_id) DO UPDATE SET
            display_name=excluded.display_name,
            book_type=excluded.book_type,
            etf_codes_json=excluded.etf_codes_json,
            notes=excluded.notes,
            is_active=excluded.is_active,
            synced_at=excluded.synced_at
    """
    book_defaults = {
        "etf_codes_json": None,
        "notes": None,
        "is_active": 1,
    }
    payload = [{**book_defaults, **r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def replace_portfolio_positions(
    conn: sqlite3.Connection,
    book_id: str,
    rows: list[dict],
) -> int:
    """以 YAML/手動匯入覆寫單一帳本持倉（全量替換）。"""
    synced_at = utc_now_iso()
    conn.execute("DELETE FROM portfolio_positions WHERE book_id = ?", (book_id,))
    if not rows:
        conn.commit()
        return 0
    sql = """
        INSERT INTO portfolio_positions (
            book_id, symbol, asset_type, stock_name, shares, cost_basis,
            entry_date, market_value, weight_pct, notes, synced_at
        ) VALUES (
            :book_id, :symbol, :asset_type, :stock_name, :shares, :cost_basis,
            :entry_date, :market_value, :weight_pct, :notes, :synced_at
        )
    """
    defaults = {
        "stock_name": None,
        "shares": None,
        "cost_basis": None,
        "entry_date": None,
        "market_value": None,
        "weight_pct": None,
        "notes": None,
    }
    payload = [{**defaults, **r, "book_id": book_id, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_portfolio_books(
    conn: sqlite3.Connection,
    *,
    active_only: bool = True,
) -> list[sqlite3.Row]:
    try:
        sql = "SELECT * FROM portfolio_books"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY book_id"
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []


def load_portfolio_positions(
    conn: sqlite3.Connection,
    book_id: str,
) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            """
            SELECT * FROM portfolio_positions
            WHERE book_id = ?
            ORDER BY asset_type, symbol
            """,
            (book_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
