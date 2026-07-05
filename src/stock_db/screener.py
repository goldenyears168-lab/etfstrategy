"""Screener / backtest raw tables · futures, shareholding, dividend, mcap, technical."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from stock_db.util import utc_now_iso


@dataclass(frozen=True)
class ShareholdingCoverage:
    stock_id: str
    min_date: str | None
    max_date: str | None
    count_window: int


@dataclass(frozen=True)
class MarketValueCoverage:
    stock_id: str
    min_date: str | None
    max_date: str | None
    count_window: int


@dataclass(frozen=True)
class TechnicalCoverage:
    stock_id: str
    min_date: str | None
    max_date: str | None
    count_window: int


def upsert_futures_institutional_daily(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO futures_institutional_daily (
            futures_id, trade_date, inst_name,
            long_oi_vol, short_oi_vol, net_oi_vol,
            long_deal_vol, short_deal_vol, net_deal_vol,
            source, synced_at
        ) VALUES (
            :futures_id, :trade_date, :inst_name,
            :long_oi_vol, :short_oi_vol, :net_oi_vol,
            :long_deal_vol, :short_deal_vol, :net_deal_vol,
            :source, :synced_at
        )
        ON CONFLICT(futures_id, trade_date, inst_name, source) DO UPDATE SET
            long_oi_vol=excluded.long_oi_vol,
            short_oi_vol=excluded.short_oi_vol,
            net_oi_vol=excluded.net_oi_vol,
            long_deal_vol=excluded.long_deal_vol,
            short_deal_vol=excluded.short_deal_vol,
            net_deal_vol=excluded.net_deal_vol,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_shareholding_daily(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_shareholding_daily (
            stock_id, trade_date, foreign_remaining_shares, foreign_remaining_ratio,
            shares_issued, capital_ntd, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :foreign_remaining_shares, :foreign_remaining_ratio,
            :shares_issued, :capital_ntd, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            foreign_remaining_shares=excluded.foreign_remaining_shares,
            foreign_remaining_ratio=excluded.foreign_remaining_ratio,
            shares_issued=excluded.shares_issued,
            capital_ntd=excluded.capital_ntd,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_dividend_history(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_dividend_history (
            stock_id, fiscal_year, ex_cash_date, cash_dividend, stock_dividend,
            payment_date, announcement_date, source, synced_at
        ) VALUES (
            :stock_id, :fiscal_year, :ex_cash_date, :cash_dividend, :stock_dividend,
            :payment_date, :announcement_date, :source, :synced_at
        )
        ON CONFLICT(stock_id, ex_cash_date, source) DO UPDATE SET
            fiscal_year=excluded.fiscal_year,
            cash_dividend=excluded.cash_dividend,
            stock_dividend=excluded.stock_dividend,
            payment_date=excluded.payment_date,
            announcement_date=excluded.announcement_date,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_market_value_daily(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_market_value_daily (
            stock_id, trade_date, market_value_ntd, mcap_to_revenue_ttm,
            source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :market_value_ntd, :mcap_to_revenue_ttm,
            :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            market_value_ntd=excluded.market_value_ntd,
            mcap_to_revenue_ttm=excluded.mcap_to_revenue_ttm,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_technical_daily(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_technical_daily (
            stock_id, trade_date, ma5, ma10, ma20, ma60,
            return_1d_pct, return_5d_pct, vol_avg_5d, vol_ratio_5d,
            kd_k, kd_d, kd_cross_above_20, high_120d, is_120d_high,
            source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :ma5, :ma10, :ma20, :ma60,
            :return_1d_pct, :return_5d_pct, :vol_avg_5d, :vol_ratio_5d,
            :kd_k, :kd_d, :kd_cross_above_20, :high_120d, :is_120d_high,
            :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            ma5=excluded.ma5, ma10=excluded.ma10, ma20=excluded.ma20, ma60=excluded.ma60,
            return_1d_pct=excluded.return_1d_pct, return_5d_pct=excluded.return_5d_pct,
            vol_avg_5d=excluded.vol_avg_5d, vol_ratio_5d=excluded.vol_ratio_5d,
            kd_k=excluded.kd_k, kd_d=excluded.kd_d,
            kd_cross_above_20=excluded.kd_cross_above_20,
            high_120d=excluded.high_120d, is_120d_high=excluded.is_120d_high,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def _load_coverage_map(
    conn: sqlite3.Connection,
    table: str,
    stock_ids: list[str],
    *,
    window_start: str,
    window_end: str,
    coverage_cls: type,
) -> dict[str, ShareholdingCoverage | MarketValueCoverage | TechnicalCoverage]:
    if not stock_ids:
        return {}
    placeholders = ",".join("?" * len(stock_ids))
    try:
        rows = conn.execute(
            f"""
            SELECT stock_id,
                   MIN(trade_date) AS d_min,
                   MAX(trade_date) AS d_max,
                   COUNT(*) AS cnt
            FROM {table}
            WHERE stock_id IN ({placeholders})
              AND source IN ('finmind', 'computed')
              AND trade_date >= ? AND trade_date <= ?
            GROUP BY stock_id
            """,
            [*stock_ids, window_start, window_end],
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict = {}
    for row in rows:
        out[str(row["stock_id"])] = coverage_cls(
            stock_id=str(row["stock_id"]),
            min_date=row["d_min"],
            max_date=row["d_max"],
            count_window=int(row["cnt"] or 0),
        )
    return out


def load_shareholding_coverage_map(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    window_start: str,
    window_end: str,
) -> dict[str, ShareholdingCoverage]:
    return _load_coverage_map(
        conn,
        "stock_shareholding_daily",
        stock_ids,
        window_start=window_start,
        window_end=window_end,
        coverage_cls=ShareholdingCoverage,
    )


def load_market_value_coverage_map(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    window_start: str,
    window_end: str,
) -> dict[str, MarketValueCoverage]:
    return _load_coverage_map(
        conn,
        "stock_market_value_daily",
        stock_ids,
        window_start=window_start,
        window_end=window_end,
        coverage_cls=MarketValueCoverage,
    )


def load_technical_coverage_map(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    window_start: str,
    window_end: str,
) -> dict[str, TechnicalCoverage]:
    return _load_coverage_map(
        conn,
        "stock_technical_daily",
        stock_ids,
        window_start=window_start,
        window_end=window_end,
        coverage_cls=TechnicalCoverage,
    )


def load_futures_institutional_coverage(
    conn: sqlite3.Connection,
    futures_id: str,
    *,
    window_start: str,
    window_end: str,
) -> tuple[str | None, str | None, int]:
    try:
        row = conn.execute(
            """
            SELECT MIN(trade_date) AS d_min, MAX(trade_date) AS d_max, COUNT(*) AS cnt
            FROM futures_institutional_daily
            WHERE futures_id = ? AND source = 'finmind'
              AND trade_date >= ? AND trade_date <= ?
            """,
            (futures_id, window_start, window_end),
        ).fetchone()
    except sqlite3.OperationalError:
        return None, None, 0
    if row is None:
        return None, None, 0
    return row["d_min"], row["d_max"], int(row["cnt"] or 0)
