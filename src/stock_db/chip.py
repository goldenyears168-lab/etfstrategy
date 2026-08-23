"""Margin, lending, daytrade, branch, block-trade chip data."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from stock_db.util import utc_now_iso

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

def _chip_coverage_sql(table: str) -> str:
    return f"""
        SELECT stock_id, MIN(trade_date) AS d_min, MAX(trade_date) AS d_max, COUNT(*) AS n
        FROM {table}
        WHERE source = ? AND trade_date >= ? AND trade_date <= ?
          AND stock_id IN ({{placeholders}})
        GROUP BY stock_id
    """


def load_stock_chip_coverage_map(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    window_start: str,
    window_end: str,
    source: str = "finmind",
) -> dict[str, StockChipCoverage]:
    if not stock_ids:
        return {}
    placeholders = ",".join("?" * len(stock_ids))
    params = [source, window_start, window_end, *stock_ids]

    def _rows(table: str) -> dict[str, tuple[str | None, str | None, int]]:
        sql = _chip_coverage_sql(table).format(placeholders=placeholders)
        fetched = conn.execute(sql, params).fetchall()
        return {
            r["stock_id"]: (r["d_min"], r["d_max"], int(r["n"])) for r in fetched
        }

    margin = _rows("stock_margin_daily")
    lending = _rows("stock_lending_daily")
    daytrade = _rows("stock_daytrade_daily")
    out: dict[str, StockChipCoverage] = {}
    for sid in stock_ids:
        m = margin.get(sid, (None, None, 0))
        l = lending.get(sid, (None, None, 0))
        d = daytrade.get(sid, (None, None, 0))
        out[sid] = StockChipCoverage(
            stock_id=sid,
            margin_min=m[0],
            margin_max=m[1],
            margin_count_window=m[2],
            lending_min=l[0],
            lending_max=l[1],
            lending_count_window=l[2],
            daytrade_min=d[0],
            daytrade_max=d[1],
            daytrade_count_window=d[2],
        )
    return out


def upsert_stock_margin_daily(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_margin_daily (
            stock_id, trade_date, margin_balance, margin_change,
            short_balance, short_change, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :margin_balance, :margin_change,
            :short_balance, :short_change, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            margin_balance=excluded.margin_balance,
            margin_change=excluded.margin_change,
            short_balance=excluded.short_balance,
            short_change=excluded.short_change,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_lending_daily(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_lending_daily (
            stock_id, trade_date, lending_balance, lending_change, fee_rate,
            source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :lending_balance, :lending_change, :fee_rate,
            :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            lending_balance=excluded.lending_balance,
            lending_change=excluded.lending_change,
            fee_rate=excluded.fee_rate,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_daytrade_daily(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_daytrade_daily (
            stock_id, trade_date, daytrade_volume, total_volume,
            daytrade_ratio_pct, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :daytrade_volume, :total_volume,
            :daytrade_ratio_pct, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            daytrade_volume=excluded.daytrade_volume,
            total_volume=excluded.total_volume,
            daytrade_ratio_pct=excluded.daytrade_ratio_pct,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_branch_daily(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_branch_daily (
            stock_id, trade_date, buy_top5_net, sell_top5_net,
            smart_net, retail_net, branch_count, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :buy_top5_net, :sell_top5_net,
            :smart_net, :retail_net, :branch_count, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            buy_top5_net=excluded.buy_top5_net,
            sell_top5_net=excluded.sell_top5_net,
            smart_net=excluded.smart_net,
            retail_net=excluded.retail_net,
            branch_count=excluded.branch_count,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_broker_branch_daily(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Persist per-branch (securities_trader) buy/sell aggregates by stock/date."""
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_broker_branch_daily (
            trade_date, securities_trader_id, securities_trader, stock_id,
            buy, sell, net, source, synced_at
        ) VALUES (
            :trade_date, :securities_trader_id, :securities_trader, :stock_id,
            :buy, :sell, :net, :source, :synced_at
        )
        ON CONFLICT(trade_date, securities_trader_id, stock_id, source) DO UPDATE SET
            securities_trader=excluded.securities_trader,
            buy=excluded.buy,
            sell=excluded.sell,
            net=excluded.net,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def list_broker_branch_tape_dates(
    conn: sqlite3.Connection,
    securities_trader_id: str,
    *,
    source: str = "finmind",
    window_start: str | None = None,
    window_end: str | None = None,
) -> list[str]:
    sql = """
        SELECT DISTINCT trade_date
        FROM stock_broker_branch_daily
        WHERE securities_trader_id = ? AND source = ?
    """
    params: list[object] = [securities_trader_id, source]
    if window_start:
        sql += " AND trade_date >= ?"
        params.append(window_start)
    if window_end:
        sql += " AND trade_date <= ?"
        params.append(window_end)
    sql += " ORDER BY trade_date ASC"
    return [str(r[0]) for r in conn.execute(sql, params).fetchall()]


def load_broker_branch_nets_for_date(
    conn: sqlite3.Connection,
    securities_trader_id: str,
    trade_date: str,
    *,
    source: str = "finmind",
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT stock_id, securities_trader, buy, sell, net
        FROM stock_broker_branch_daily
        WHERE securities_trader_id = ? AND trade_date = ? AND source = ?
        ORDER BY net DESC, stock_id ASC
        """,
        (securities_trader_id, trade_date, source),
    ).fetchall()


def load_broker_branch_nets(
    conn: sqlite3.Connection,
    securities_trader_id: str,
    *,
    source: str = "finmind",
    window_start: str | None = None,
    window_end: str | None = None,
    min_net: float | None = None,
) -> list[sqlite3.Row]:
    sql = """
        SELECT trade_date, stock_id, securities_trader, buy, sell, net
        FROM stock_broker_branch_daily
        WHERE securities_trader_id = ? AND source = ?
    """
    params: list[object] = [securities_trader_id, source]
    if window_start:
        sql += " AND trade_date >= ?"
        params.append(window_start)
    if window_end:
        sql += " AND trade_date <= ?"
        params.append(window_end)
    if min_net is not None:
        sql += " AND net >= ?"
        params.append(min_net)
    sql += " ORDER BY trade_date ASC, net DESC, stock_id ASC"
    return conn.execute(sql, params).fetchall()


def upsert_stock_block_trade(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_block_trade (
            stock_id, trade_date, block_volume, block_amount,
            block_count, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :block_volume, :block_amount,
            :block_count, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            block_volume=excluded.block_volume,
            block_amount=excluded.block_amount,
            block_count=excluded.block_count,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_lending_balance_daily(
    conn: sqlite3.Connection, rows: list[dict]
) -> int:
    """TWSE TWT72U 借券餘額合計表 → stock_lending_balance_daily。"""
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_lending_balance_daily (
            stock_id, trade_date, prev_balance, borrow_volume, return_volume,
            lending_balance, close, market_value, market, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :prev_balance, :borrow_volume, :return_volume,
            :lending_balance, :close, :market_value, :market, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            prev_balance=excluded.prev_balance,
            borrow_volume=excluded.borrow_volume,
            return_volume=excluded.return_volume,
            lending_balance=excluded.lending_balance,
            close=excluded.close,
            market_value=excluded.market_value,
            market=excluded.market,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_holding_dispersion_weekly(
    conn: sqlite3.Connection, rows: list[dict]
) -> int:
    """FinMind TaiwanStockHoldingSharesPer 股權分散表 → 週頻大戶／散戶結構。"""
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_holding_dispersion_weekly (
            stock_id, as_of_date, level, level_lo, level_hi,
            people, shares, percent, source, synced_at
        ) VALUES (
            :stock_id, :as_of_date, :level, :level_lo, :level_hi,
            :people, :shares, :percent, :source, :synced_at
        )
        ON CONFLICT(stock_id, as_of_date, level, source) DO UPDATE SET
            level_lo=excluded.level_lo,
            level_hi=excluded.level_hi,
            people=excluded.people,
            shares=excluded.shares,
            percent=excluded.percent,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def refresh_daytrade_ratio(
    conn: sqlite3.Connection,
    *,
    stock_ids: list[str] | None = None,
    start_date: str | None = None,
) -> int:
    """用 stock_daily_bars.volume 當分母補算現沖比例。

    FinMind ``TaiwanStockDayTrading`` 只給當沖成交股數，全日總量得自己接。
    現沖比例＝當沖量／全日成交量，是判斷「這檔的量有多少會沉澱成籌碼」的
    關鍵分母；比例站上 50% 時，任何依賴成交量的籌碼訊號都應降權。
    """
    where = ["d.daytrade_volume IS NOT NULL", "b.volume IS NOT NULL", "b.volume > 0"]
    params: list[object] = []
    if stock_ids:
        where.append(f"d.stock_id IN ({','.join('?' * len(stock_ids))})")
        params.extend(stock_ids)
    if start_date:
        where.append("d.trade_date >= ?")
        params.append(start_date)
    sql = f"""
        UPDATE stock_daytrade_daily AS d
           SET total_volume = b.volume,
               daytrade_ratio_pct = ROUND(d.daytrade_volume * 100.0 / b.volume, 2)
          FROM stock_daily_bars AS b
         WHERE b.stock_id = d.stock_id
           AND b.trade_date = d.trade_date
           AND {' AND '.join(where)}
    """
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


def upsert_stock_short_interest_daily(
    conn: sqlite3.Connection, rows: list[dict]
) -> int:
    """TWSE TWT93U 信用額度總量管制餘額表 → 融券 ＋ 借券賣出餘額。

    ``sbl_balance``（借券賣出當日餘額）才是對應學術文獻 short interest 的欄位；
    ``stock_lending_balance_daily.lending_balance``（TWT72U 借券餘額）包含 ETF
    造市／避險／套利等非方向性用途，2408 在 2026-08 期間只有 47~63% 是真放空，
    且該比例逐日下滑 —— 直接拿它當空單代理會給出反向訊號。
    """
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_short_interest_daily (
            stock_id, trade_date, short_prev, short_sell, short_buy,
            short_cash_offset, short_balance, short_limit,
            sbl_prev, sbl_sell, sbl_return, sbl_adjust, sbl_balance,
            sbl_next_limit, note, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :short_prev, :short_sell, :short_buy,
            :short_cash_offset, :short_balance, :short_limit,
            :sbl_prev, :sbl_sell, :sbl_return, :sbl_adjust, :sbl_balance,
            :sbl_next_limit, :note, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            short_prev=excluded.short_prev, short_sell=excluded.short_sell,
            short_buy=excluded.short_buy,
            short_cash_offset=excluded.short_cash_offset,
            short_balance=excluded.short_balance, short_limit=excluded.short_limit,
            sbl_prev=excluded.sbl_prev, sbl_sell=excluded.sbl_sell,
            sbl_return=excluded.sbl_return, sbl_adjust=excluded.sbl_adjust,
            sbl_balance=excluded.sbl_balance,
            sbl_next_limit=excluded.sbl_next_limit,
            note=excluded.note, synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)
