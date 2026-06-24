"""ETF/index bars, TW stock market data, intraday, coverage."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from stock_db.util import utc_now_iso

def _daily_bar_payload(rows: list[dict], synced_at: str) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "code": r["code"],
                "date": r["date"],
                "open": r.get("open"),
                "high": r.get("high"),
                "low": r.get("low"),
                "close": r["close"],
                "adj_close": r.get("adj_close"),
                "volume": r.get("volume"),
                "spread": r.get("spread"),
                "source": r["source"],
                "synced_at": synced_at,
            }
        )
    return out


def upsert_daily_bars(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO daily_bars (
            code, date, open, high, low, close, adj_close, volume, spread, source, synced_at
        ) VALUES (
            :code, :date, :open, :high, :low, :close, :adj_close, :volume, :spread, :source, :synced_at
        )
        ON CONFLICT(code, date, source) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, adj_close=excluded.adj_close,
            volume=excluded.volume, spread=excluded.spread,
            synced_at=excluded.synced_at
    """
    payload = _daily_bar_payload(rows, synced_at)
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


def load_order_tx_gap(
    conn: sqlite3.Connection,
    trade_date: str | None = None,
) -> tuple[float | None, str]:
    """下單層台指 gap：優先 morning 即時，fallback tech_risk 隔夜。"""
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
            stock_id, trade_date, open, high, low, close, adj_close, volume, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :open, :high, :low, :close, :adj_close, :volume, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, adj_close=excluded.adj_close, volume=excluded.volume,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "adj_close": r.get("adj_close"), "synced_at": synced_at} for r in rows]
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
    bar_min: str | None
    bar_max: str | None
    bar_count_window: int
    inst_min: str | None
    inst_max: str | None
    inst_count_window: int


@dataclass(frozen=True)
class StockChipCoverage:
    stock_id: str
    margin_min: str | None
    margin_max: str | None
    margin_count_window: int
    lending_min: str | None
    lending_max: str | None
    lending_count_window: int
    daytrade_min: str | None
    daytrade_max: str | None
    daytrade_count_window: int


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
        SELECT stock_id, MIN(trade_date) AS bar_min, MAX(trade_date) AS bar_max, COUNT(*) AS n
        FROM stock_daily_bars
        WHERE source = ? AND trade_date >= ? AND trade_date <= ?
          AND stock_id IN ({placeholders})
        GROUP BY stock_id
        """,
        params,
    ).fetchall()
    inst_rows = conn.execute(
        f"""
        SELECT stock_id, MIN(trade_date) AS inst_min, MAX(trade_date) AS inst_max, COUNT(*) AS n
        FROM stock_institutional_daily
        WHERE source = ? AND trade_date >= ? AND trade_date <= ?
          AND stock_id IN ({placeholders})
        GROUP BY stock_id
        """,
        params,
    ).fetchall()
    bar_by_id = {
        r["stock_id"]: (r["bar_min"], r["bar_max"], int(r["n"])) for r in bar_rows
    }
    inst_by_id = {
        r["stock_id"]: (r["inst_min"], r["inst_max"], int(r["n"])) for r in inst_rows
    }
    out: dict[str, StockMarketCoverage] = {}
    for sid in stock_ids:
        b = bar_by_id.get(sid, (None, None, 0))
        i = inst_by_id.get(sid, (None, None, 0))
        out[sid] = StockMarketCoverage(
            stock_id=sid,
            bar_min=b[0],
            bar_max=b[1],
            bar_count_window=b[2],
            inst_min=i[0],
            inst_max=i[1],
            inst_count_window=i[2],
        )
    return out


def count_stock_market_rows(conn: sqlite3.Connection) -> tuple[int, int, str | None, str | None]:
    """(bars 筆數, institutional 筆數, 最新 bar 日, 最新法人日)"""
    bar_n = conn.execute("SELECT COUNT(*) FROM stock_daily_bars").fetchone()[0]
    inst_n = conn.execute("SELECT COUNT(*) FROM stock_institutional_daily").fetchone()[0]
    bar_max = conn.execute("SELECT MAX(trade_date) FROM stock_daily_bars").fetchone()[0]
    inst_max = conn.execute("SELECT MAX(trade_date) FROM stock_institutional_daily").fetchone()[0]
    return int(bar_n), int(inst_n), bar_max, inst_max

def load_stock_opening_session_stats(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM stock_opening_session_stats
        WHERE stock_id = ? AND trade_date = ?
        """,
        (stock_id, trade_date),
    ).fetchone()


def persist_stock_opening_session_stats(
    conn: sqlite3.Connection,
    rows: list[dict],
) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_opening_session_stats (
            stock_id, trade_date, vol_0905_0915, px_0915, px_0905,
            n_ticks_window, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :vol_0905_0915, :px_0915, :px_0905,
            :n_ticks_window, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date) DO UPDATE SET
            vol_0905_0915=excluded.vol_0905_0915,
            px_0915=excluded.px_0915,
            px_0905=excluded.px_0905,
            n_ticks_window=excluded.n_ticks_window,
            source=excluded.source,
            synced_at=excluded.synced_at
    """
    payload = [
        {
            "stock_id": r["stock_id"],
            "trade_date": r["trade_date"],
            "vol_0905_0915": r.get("vol_0905_0915"),
            "px_0915": r.get("px_0915"),
            "px_0905": r.get("px_0905"),
            "n_ticks_window": int(r.get("n_ticks_window") or 0),
            "source": r.get("source") or "finmind_tick",
            "synced_at": r.get("synced_at") or synced_at,
        }
        for r in rows
    ]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)
def upsert_us_daily_bars(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO us_daily_bars (
            ticker, trade_date, open, high, low, close, adj_close, volume, source, synced_at
        ) VALUES (
            :ticker, :trade_date, :open, :high, :low, :close, :adj_close, :volume, :source, :synced_at
        )
        ON CONFLICT(ticker, trade_date, source) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, adj_close=excluded.adj_close, volume=excluded.volume,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "adj_close": r.get("adj_close"), "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_us_daily_bars(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    start: str,
    end: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM us_daily_bars
        WHERE ticker = ? AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date ASC, CASE source WHEN 'finmind' THEN 0 ELSE 1 END
        """,
        (ticker.upper(), start, end),
    ).fetchall()
def _table_window_stats(
    conn: sqlite3.Connection,
    table: str,
    *,
    window_start: str,
    window_end: str,
    date_col: str = "trade_date",
    source: str | None = "finmind",
) -> dict[str, object]:
    try:
        if source:
            row = conn.execute(
                f"""
                SELECT COUNT(DISTINCT stock_id) AS stocks, COUNT(*) AS rows,
                       MIN({date_col}) AS d_min, MAX({date_col}) AS d_max
                FROM {table}
                WHERE source = ? AND {date_col} >= ? AND {date_col} <= ?
                """,
                (source, window_start, window_end),
            ).fetchone()
        else:
            row = conn.execute(
                f"""
                SELECT COUNT(DISTINCT code) AS stocks, COUNT(*) AS rows,
                       MIN(date) AS d_min, MAX(date) AS d_max
                FROM {table}
                WHERE date >= ? AND date <= ?
                """,
                (window_start, window_end),
            ).fetchone()
    except sqlite3.OperationalError:
        return {"stocks": 0, "rows": 0, "min": None, "max": None}
    if row is None:
        return {"stocks": 0, "rows": 0, "min": None, "max": None}
    return {
        "stocks": int(row["stocks"] or 0),
        "rows": int(row["rows"] or 0),
        "min": row["d_min"],
        "max": row["d_max"],
    }


def market_data_coverage_summary(
    conn: sqlite3.Connection,
    *,
    window_start: str,
    window_end: str,
) -> dict[str, object]:
    return {
        "window_start": window_start,
        "window_end": window_end,
        "etf_bars": _table_window_stats(
            conn, "daily_bars", window_start=window_start, window_end=window_end, source=None
        ),
        "stock_market_bars": _table_window_stats(
            conn, "stock_daily_bars", window_start=window_start, window_end=window_end
        ),
        "stock_institutional": _table_window_stats(
            conn, "stock_institutional_daily", window_start=window_start, window_end=window_end
        ),
        "chip_margin": _table_window_stats(
            conn, "stock_margin_daily", window_start=window_start, window_end=window_end
        ),
        "chip_lending": _table_window_stats(
            conn, "stock_lending_daily", window_start=window_start, window_end=window_end
        ),
        "chip_daytrade": _table_window_stats(
            conn, "stock_daytrade_daily", window_start=window_start, window_end=window_end
        ),
    }


def format_market_data_coverage(summary: dict[str, object]) -> str:
    lines = [
        f"Market data coverage · {summary['window_start']} .. {summary['window_end']}",
        "",
    ]
    labels = {
        "etf_bars": "ETF/index daily_bars",
        "stock_market_bars": "stock_daily_bars",
        "stock_institutional": "stock_institutional_daily",
        "chip_margin": "stock_margin_daily",
        "chip_lending": "stock_lending_daily",
        "chip_daytrade": "stock_daytrade_daily",
    }
    for key, label in labels.items():
        block = summary.get(key) or {}
        lines.append(
            f"- {label}: stocks={block.get('stocks', 0)} rows={block.get('rows', 0)} "
            f"range={block.get('min')}..{block.get('max')}"
        )
    return "\n".join(lines) + "\n"
