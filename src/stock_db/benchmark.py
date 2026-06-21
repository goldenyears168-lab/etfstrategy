"""Benchmark ETF constituents (0050 etc.) for market-data universe extension."""
from __future__ import annotations

import sqlite3

from stock_db.util import utc_now_iso


def upsert_benchmark_constituents_meta(conn: sqlite3.Connection, row: dict) -> None:
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO benchmark_constituents_meta (
            benchmark_code, snapshot_date, holding_count, source, synced_at
        ) VALUES (
            :benchmark_code, :snapshot_date, :holding_count, :source, :synced_at
        )
        ON CONFLICT(benchmark_code, snapshot_date) DO UPDATE SET
            holding_count=excluded.holding_count,
            source=excluded.source,
            synced_at=excluded.synced_at
    """
    conn.execute(sql, {**row, "synced_at": synced_at})
    conn.commit()


def upsert_benchmark_constituents(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    benchmark_code = rows[0]["benchmark_code"]
    snapshot_date = rows[0]["snapshot_date"]
    conn.execute(
        """
        DELETE FROM benchmark_constituents
        WHERE benchmark_code = ? AND snapshot_date = ?
        """,
        (benchmark_code, snapshot_date),
    )
    conn.commit()
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO benchmark_constituents (
            benchmark_code, snapshot_date, stock_id, stock_name, weight_pct,
            source, synced_at
        ) VALUES (
            :benchmark_code, :snapshot_date, :stock_id, :stock_name, :weight_pct,
            :source, :synced_at
        )
        ON CONFLICT(benchmark_code, snapshot_date, stock_id) DO UPDATE SET
            stock_name=excluded.stock_name,
            weight_pct=excluded.weight_pct,
            source=excluded.source,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def _load_benchmark_watchlist_stocks(
    conn: sqlite3.Connection,
    benchmark_codes: tuple[str, ...],
) -> dict[str, dict]:
    stocks: dict[str, dict] = {}
    for benchmark_code in benchmark_codes:
        row = conn.execute(
            """
            SELECT snapshot_date
            FROM benchmark_constituents_meta
            WHERE benchmark_code = ?
            ORDER BY snapshot_date DESC
            LIMIT 1
            """,
            (benchmark_code,),
        ).fetchone()
        if row is None:
            continue
        snapshot_date = row["snapshot_date"]
        for holding in conn.execute(
            """
            SELECT stock_id, stock_name, weight_pct
            FROM benchmark_constituents
            WHERE benchmark_code = ? AND snapshot_date = ?
            ORDER BY weight_pct DESC, stock_id
            """,
            (benchmark_code, snapshot_date),
        ):
            stock_id = holding["stock_id"]
            if not stock_id:
                continue
            entry = stocks.setdefault(
                stock_id,
                {
                    "stock_id": stock_id,
                    "stock_name": "",
                    "benchmark_hold_count": 0,
                    "_benchmark_codes": set(),
                },
            )
            entry["_benchmark_codes"].add(benchmark_code)
            if holding["stock_name"]:
                entry["stock_name"] = holding["stock_name"]
    for entry in stocks.values():
        entry["benchmark_hold_count"] = len(entry.pop("_benchmark_codes"))
    return stocks
