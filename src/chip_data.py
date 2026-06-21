"""融資／借券／當沖快照載入（只讀 DB · 供 crowd/short/spec 子分）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

SOURCE = "finmind"


@dataclass(frozen=True)
class ChipSnapshot:
    stock_id: str
    trade_date: str | None
    margin_balance: float | None
    margin_change: float | None
    short_balance: float | None
    short_change: float | None
    lending_balance: float | None
    lending_change: float | None
    daytrade_ratio_pct: float | None
    branch_smart_net: float | None
    branch_retail_net: float | None
    block_count: int | None


def _float_or_none(value: object) -> float | None:
    """FinMind 數值欄；空字串與證交所註記（Y／＊／X）視為缺值。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_margin_rows(stock_id: str, raw: list[dict]) -> list[dict]:
    """FinMind TaiwanStockMarginPurchaseShortSale → stock_margin_daily 列。"""
    rows: list[dict] = []
    for item in raw:
        trade_date = str(item.get("date") or item.get("Date") or "")[:10]
        if not trade_date:
            continue
        mb = _float_or_none(
            item.get("MarginPurchaseTodayBalance")
            or item.get("margin_purchase_today_balance")
        )
        sb = _float_or_none(
            item.get("ShortSaleTodayBalance") or item.get("short_sale_today_balance")
        )
        mb_prev = _float_or_none(
            item.get("MarginPurchaseYesterdayBalance")
            or item.get("margin_purchase_yesterday_balance")
        )
        sb_prev = _float_or_none(
            item.get("ShortSaleYesterdayBalance")
            or item.get("short_sale_yesterday_balance")
        )
        m_chg = (mb - mb_prev) if mb is not None and mb_prev is not None else None
        s_chg = (sb - sb_prev) if sb is not None and sb_prev is not None else None
        rows.append(
            {
                "stock_id": stock_id,
                "trade_date": trade_date,
                "margin_balance": mb,
                "margin_change": m_chg,
                "short_balance": sb,
                "short_change": s_chg,
                "source": SOURCE,
            }
        )
    return rows


def parse_lending_rows(stock_id: str, raw: list[dict]) -> list[dict]:
    """FinMind TaiwanStockSecuritiesLending → stock_lending_daily 列。"""
    rows: list[dict] = []
    prev_bal: float | None = None
    sorted_raw = sorted(
        raw,
        key=lambda x: str(x.get("date") or x.get("Date") or ""),
    )
    for item in sorted_raw:
        trade_date = str(item.get("date") or item.get("Date") or "")[:10]
        if not trade_date:
            continue
        bal = _float_or_none(
            item.get("lending_volume_balance")
            or item.get("LendingVolumeBalance")
            or item.get("volume")
            or item.get("Volume")
        )
        fee = _float_or_none(item.get("fee_rate") or item.get("FeeRate"))
        chg = (bal - prev_bal) if bal is not None and prev_bal is not None else None
        if bal is not None:
            prev_bal = bal
        rows.append(
            {
                "stock_id": stock_id,
                "trade_date": trade_date,
                "lending_balance": bal,
                "lending_change": chg,
                "fee_rate": fee,
                "source": SOURCE,
            }
        )
    return rows


def parse_daytrade_rows(stock_id: str, raw: list[dict]) -> list[dict]:
    """FinMind TaiwanStockDayTrading → stock_daytrade_daily 列。"""
    rows: list[dict] = []
    for item in raw:
        trade_date = str(item.get("date") or item.get("Date") or "")[:10]
        if not trade_date:
            continue
        dt_vol = _float_or_none(
            item.get("BuyAfterSale")
            or item.get("buy_after_sale")
            or item.get("DayTradingVolume")
            or item.get("day_trading_volume")
        )
        total = _float_or_none(
            item.get("Volume")
            or item.get("volume")
            or item.get("Trading_Volume")
        )
        ratio = None
        if dt_vol is not None and total and total > 0:
            ratio = round(dt_vol / total * 100.0, 2)
        rows.append(
            {
                "stock_id": stock_id,
                "trade_date": trade_date,
                "daytrade_volume": dt_vol,
                "total_volume": total,
                "daytrade_ratio_pct": ratio,
                "source": SOURCE,
            }
        )
    return rows


def load_chip_snapshot(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    trade_date: str | None = None,
    source: str = SOURCE,
) -> ChipSnapshot | None:
    """合併最新（或指定日）融資／借券／當沖／分點／鉅額。"""
    date_clause = "AND trade_date = ?" if trade_date else ""
    order = "ORDER BY trade_date DESC LIMIT 1"
    params: tuple = (stock_id, trade_date) if trade_date else (stock_id,)

    def _row(table: str, cols: str) -> sqlite3.Row | None:
        try:
            return conn.execute(
                f"""
                SELECT {cols}
                FROM {table}
                WHERE stock_id = ? AND source = 'finmind' {date_clause}
                {order}
                """,
                params,
            ).fetchone()
        except sqlite3.OperationalError:
            return None

    margin = _row(
        "stock_margin_daily",
        "trade_date, margin_balance, margin_change, short_balance, short_change",
    )
    lending = _row(
        "stock_lending_daily",
        "trade_date, lending_balance, lending_change",
    )
    daytrade = _row("stock_daytrade_daily", "trade_date, daytrade_ratio_pct")
    branch = _row("stock_branch_daily", "trade_date, smart_net, retail_net")
    block = _row("stock_block_trade", "trade_date, block_count")

    if not any((margin, lending, daytrade, branch, block)):
        return None

    td = trade_date
    if td is None:
        for r in (margin, lending, daytrade, branch, block):
            if r is not None:
                td = str(r["trade_date"])
                break

    return ChipSnapshot(
        stock_id=stock_id,
        trade_date=td,
        margin_balance=_float_or_none(margin["margin_balance"]) if margin else None,
        margin_change=_float_or_none(margin["margin_change"]) if margin else None,
        short_balance=_float_or_none(margin["short_balance"]) if margin else None,
        short_change=_float_or_none(margin["short_change"]) if margin else None,
        lending_balance=_float_or_none(lending["lending_balance"]) if lending else None,
        lending_change=_float_or_none(lending["lending_change"]) if lending else None,
        daytrade_ratio_pct=_float_or_none(daytrade["daytrade_ratio_pct"])
        if daytrade
        else None,
        branch_smart_net=_float_or_none(branch["smart_net"]) if branch else None,
        branch_retail_net=_float_or_none(branch["retail_net"]) if branch else None,
        block_count=int(block["block_count"]) if block and block["block_count"] else None,
    )
