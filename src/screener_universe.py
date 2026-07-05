"""Universe resolution for screener / backtest data sync."""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from project_config import SUPPLEMENTAL_WATCHLIST_STOCKS
from stock_db import connect, load_etf_constituent_watchlist
from stock_universe import TW100_UNIVERSE_ID, resolve_universe_watchlist


def resolve_sync_watchlist(
    conn: sqlite3.Connection,
    universe: str,
    *,
    universe_as_of: date | None = None,
    stock_ids: list[str] | None = None,
    end: date | None = None,
) -> list[dict[str, Any]]:
    """etf_watchlist | tw100 | both (union by stock_id)."""
    if stock_ids:
        placeholders = ",".join("?" * len(stock_ids))
        name_rows = conn.execute(
            f"""
            SELECT stock_id, MAX(stock_name) AS stock_name
            FROM (
                SELECT stock_id, stock_name FROM etf_holdings
                WHERE stock_id IN ({placeholders})
                UNION ALL
                SELECT stock_id, stock_name FROM benchmark_constituents
                WHERE stock_id IN ({placeholders})
            )
            GROUP BY stock_id
            """,
            stock_ids + stock_ids,
        ).fetchall()
        name_by_id = {str(r["stock_id"]): r["stock_name"] or "" for r in name_rows}
        for sid in stock_ids:
            if not name_by_id.get(sid):
                name_by_id[sid] = SUPPLEMENTAL_WATCHLIST_STOCKS.get(sid, "")
        return [
            {"stock_id": sid, "stock_name": name_by_id.get(sid, "")} for sid in stock_ids
        ]

    ref = universe_as_of or end or date.today()
    if universe == "both":
        etf = load_etf_constituent_watchlist(conn)
        tw = resolve_universe_watchlist(conn, TW100_UNIVERSE_ID, as_of=ref, refresh=False)
        if not tw:
            tw = resolve_universe_watchlist(conn, TW100_UNIVERSE_ID, as_of=ref, refresh=True)
        by_id: dict[str, dict[str, Any]] = {}
        for item in etf + tw:
            sid = str(item["stock_id"])
            if sid not in by_id:
                by_id[sid] = {"stock_id": sid, "stock_name": item.get("stock_name") or ""}
        return list(by_id.values())

    if universe == TW100_UNIVERSE_ID:
        watch = resolve_universe_watchlist(conn, TW100_UNIVERSE_ID, as_of=ref, refresh=False)
        if not watch:
            watch = resolve_universe_watchlist(conn, TW100_UNIVERSE_ID, as_of=ref, refresh=True)
        return watch

    if universe == "etf_watchlist":
        return load_etf_constituent_watchlist(conn)

    raise RuntimeError(f"unknown universe: {universe} (etf_watchlist | tw100 | both)")


def load_watchlist_for_sync(
    db_path,
    universe: str,
    *,
    universe_as_of: date | None = None,
    stock_ids: list[str] | None = None,
    end: date | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        return resolve_sync_watchlist(
            conn,
            universe,
            universe_as_of=universe_as_of,
            stock_ids=stock_ids,
            end=end,
        )
    finally:
        conn.close()
