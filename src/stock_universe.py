"""Universe resolution: etf_watchlist vs tw100 (FinMind market cap)."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from typing import Any

from finmind_client import fetch_finmind_dataset
from stock_db import connect, load_etf_constituent_watchlist, utc_now_iso
from universe_config import load_universe_config, tw100_config

TW100_UNIVERSE_ID = "tw100"
_MARKET_VALUE_LOOKBACK_DAYS = 10


class UniverseSnapshotError(ValueError):
    """Universe snapshot failed validation (e.g. too few constituents)."""


def _tw100_min_constituents() -> int:
    return int(tw100_config().get("min_constituent_count", 95))


def _resolve_market_value_date(as_of: date) -> date:
    """Step back until TaiwanStockMarketValue returns rows (weekends/holidays)."""
    for offset in range(_MARKET_VALUE_LOOKBACK_DAYS + 1):
        probe = as_of - timedelta(days=offset)
        rows = fetch_finmind_dataset(
            "TaiwanStockMarketValue",
            start=probe,
            end=probe,
        )
        if rows:
            return probe
    raise RuntimeError(
        f"TaiwanStockMarketValue 無資料（{as_of} 起回溯 {_MARKET_VALUE_LOOKBACK_DAYS} 日）"
    )


def fetch_tw100_constituents(
    as_of: date,
    *,
    rank_limit: int = 100,
    stock_id_pattern: str = r"^\d{4}$",
    extra_stock_ids: list[str] | None = None,
    exclude_stock_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """PIT-safe: TaiwanStockMarketValue @ as_of → top-N listed stocks by market_value."""
    snap_date = _resolve_market_value_date(as_of)
    rows = fetch_finmind_dataset(
        "TaiwanStockMarketValue",
        start=snap_date,
        end=snap_date,
    )
    pat = re.compile(stock_id_pattern)
    exclude = {str(s).strip() for s in (exclude_stock_ids or []) if str(s).strip()}
    listed = [
        r
        for r in rows
        if pat.match(str(r.get("stock_id", ""))) and str(r.get("stock_id", "")) not in exclude
    ]
    listed.sort(key=lambda r: float(r.get("market_value") or 0), reverse=True)
    top = listed[:rank_limit]
    by_id: dict[str, dict[str, Any]] = {}
    for rank, row in enumerate(top, start=1):
        sid = str(row["stock_id"])
        by_id[sid] = {
            "stock_id": sid,
            "stock_name": "",
            "rank_no": rank,
            "market_value": float(row.get("market_value") or 0),
        }
    next_rank = rank_limit + 1
    for sid in extra_stock_ids or []:
        sid = str(sid).strip()
        if not sid or sid in by_id:
            continue
        mv_row = next((r for r in listed if str(r.get("stock_id")) == sid), None)
        by_id[sid] = {
            "stock_id": sid,
            "stock_name": "",
            "rank_no": next_rank,
            "market_value": float(mv_row["market_value"]) if mv_row else None,
        }
        next_rank += 1
    return sorted(by_id.values(), key=lambda w: (w.get("rank_no") or 9999, w["stock_id"]))


def upsert_universe_snapshot(
    conn: sqlite3.Connection,
    universe_id: str,
    snapshot_date: str,
    constituents: list[dict[str, Any]],
    *,
    source: str = "finmind",
    min_constituent_count: int | None = None,
) -> int:
    if min_constituent_count is not None and len(constituents) < min_constituent_count:
        raise UniverseSnapshotError(
            f"{universe_id} snapshot {snapshot_date}: {len(constituents)} constituents "
            f"< min {min_constituent_count} — 拒絕寫入"
        )
    synced_at = utc_now_iso()
    conn.execute(
        """
        INSERT INTO universe_snapshot_meta (
            universe_id, snapshot_date, constituent_count, source, synced_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(universe_id, snapshot_date) DO UPDATE SET
            constituent_count=excluded.constituent_count,
            source=excluded.source,
            synced_at=excluded.synced_at
        """,
        (universe_id, snapshot_date, len(constituents), source, synced_at),
    )
    conn.execute(
        "DELETE FROM universe_constituents WHERE universe_id = ? AND snapshot_date = ?",
        (universe_id, snapshot_date),
    )
    if constituents:
        payload = [
            {
                "universe_id": universe_id,
                "snapshot_date": snapshot_date,
                "stock_id": c["stock_id"],
                "stock_name": c.get("stock_name") or "",
                "rank_no": c.get("rank_no"),
                "market_value": c.get("market_value"),
                "source": source,
                "synced_at": synced_at,
            }
            for c in constituents
        ]
        conn.executemany(
            """
            INSERT INTO universe_constituents (
                universe_id, snapshot_date, stock_id, stock_name,
                rank_no, market_value, source, synced_at
            ) VALUES (
                :universe_id, :snapshot_date, :stock_id, :stock_name,
                :rank_no, :market_value, :source, :synced_at
            )
            """,
            payload,
        )
    conn.commit()
    return len(constituents)


def load_universe_constituents(
    conn: sqlite3.Connection,
    universe_id: str,
    snapshot_date: str | None = None,
    *,
    min_constituent_count: int | None = None,
) -> list[dict[str, Any]]:
    if snapshot_date:
        snap = snapshot_date
    else:
        if min_constituent_count is not None:
            row = conn.execute(
                """
                SELECT snapshot_date FROM universe_snapshot_meta
                WHERE universe_id = ? AND constituent_count >= ?
                ORDER BY snapshot_date DESC
                LIMIT 1
                """,
                (universe_id, min_constituent_count),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT snapshot_date FROM universe_snapshot_meta
                WHERE universe_id = ?
                ORDER BY snapshot_date DESC
                LIMIT 1
                """,
                (universe_id,),
            ).fetchone()
        if row is None:
            return []
        snap = str(row["snapshot_date"])
    rows = conn.execute(
        """
        SELECT stock_id, stock_name, rank_no, market_value
        FROM universe_constituents
        WHERE universe_id = ? AND snapshot_date = ?
        ORDER BY COALESCE(rank_no, 9999), stock_id
        """,
        (universe_id, snap),
    ).fetchall()
    return [
        {
            "stock_id": str(r["stock_id"]),
            "stock_name": r["stock_name"] or "",
            "rank_no": r["rank_no"],
            "market_value": r["market_value"],
            "etf_hold_count": 0,
            "fund_hold_count": 0,
            "benchmark_hold_count": 0,
            "supplemental_hold_count": 0,
        }
        for r in rows
    ]


def resolve_universe_watchlist(
    conn: sqlite3.Connection,
    universe_id: str,
    *,
    as_of: date | None = None,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """Return watchlist-shaped rows for sync scripts."""
    if universe_id == "etf_watchlist":
        return load_etf_constituent_watchlist(conn)

    if universe_id != TW100_UNIVERSE_ID:
        raise ValueError(f"unknown universe: {universe_id}")

    cfg = tw100_config()
    snap_date = (as_of or date.today()).isoformat()
    if not refresh:
        cached = load_universe_constituents(conn, TW100_UNIVERSE_ID, snap_date)
        if cached:
            return cached
        # Fallback: latest qualified snapshot on or before requested date
        min_n = _tw100_min_constituents() if universe_id == TW100_UNIVERSE_ID else None
        if min_n:
            row = conn.execute(
                """
                SELECT snapshot_date FROM universe_snapshot_meta
                WHERE universe_id = ? AND snapshot_date <= ? AND constituent_count >= ?
                ORDER BY snapshot_date DESC
                LIMIT 1
                """,
                (TW100_UNIVERSE_ID, snap_date, min_n),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT snapshot_date FROM universe_snapshot_meta
                WHERE universe_id = ? AND snapshot_date <= ?
                ORDER BY snapshot_date DESC
                LIMIT 1
                """,
                (TW100_UNIVERSE_ID, snap_date),
            ).fetchone()
        if row is not None:
            cached = load_universe_constituents(conn, TW100_UNIVERSE_ID, str(row["snapshot_date"]))
            if cached:
                return cached

    effective = as_of or date.today()
    constituents = fetch_tw100_constituents(
        effective,
        rank_limit=cfg["rank_limit"],
        stock_id_pattern=cfg["stock_id_pattern"],
        extra_stock_ids=cfg["extra_stock_ids"],
        exclude_stock_ids=cfg["exclude_stock_ids"],
    )
    if not constituents:
        raise RuntimeError(f"TW100 成分為空 @ {effective}")
    # Persist under FinMind market-value date (may be earlier than as_of on holidays)
    mv_date = _resolve_market_value_date(effective).isoformat()
    upsert_universe_snapshot(
        conn,
        TW100_UNIVERSE_ID,
        mv_date,
        constituents,
        source=cfg["source"],
        min_constituent_count=cfg["min_constituent_count"],
    )
    return load_universe_constituents(conn, TW100_UNIVERSE_ID, mv_date)


def sync_tw100_snapshot(
    db_path,
    *,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        return resolve_universe_watchlist(
            conn,
            TW100_UNIVERSE_ID,
            as_of=as_of,
            refresh=True,
        )
    finally:
        conn.close()
