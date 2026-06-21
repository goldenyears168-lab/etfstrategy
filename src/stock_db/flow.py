"""ETF flow events and per-ETF legs."""
from __future__ import annotations

import sqlite3

from stock_db.util import utc_now_iso

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
_FLOW_EVENT_LEG_COLS = (
    "event_date",
    "prev_date",
    "stock_id",
    "etf_id",
    "stock_name",
    "action",
    "shares_delta",
    "value_delta",
    "weight_delta",
    "price_before_5d",
    "return_before_5d",
    "sector",
    "theme",
    "flow_tape_regime",
    "flow_version",
    "return_after_1d",
    "alpha_after_1d",
    "return_after_3d",
    "alpha_after_3d",
    "return_after_5d",
    "alpha_after_5d",
    "return_after_10d",
    "alpha_after_10d",
    "return_after_20d",
    "alpha_after_20d",
)


def upsert_flow_event_legs(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    cols = ", ".join(_FLOW_EVENT_LEG_COLS) + ", synced_at"
    placeholders = ", ".join(f":{c}" for c in _FLOW_EVENT_LEG_COLS) + ", :synced_at"
    updates = ", ".join(
        f"{c}=excluded.{c}"
        for c in _FLOW_EVENT_LEG_COLS
        if c not in ("event_date", "stock_id", "etf_id", "flow_version")
    )
    sql = f"""
        INSERT INTO flow_event_legs ({cols})
        VALUES ({placeholders})
        ON CONFLICT(event_date, stock_id, etf_id, flow_version) DO UPDATE SET
            {updates},
            synced_at=excluded.synced_at
    """
    payload = []
    for r in rows:
        item = {}
        for c in _FLOW_EVENT_LEG_COLS:
            if c == "flow_tape_regime":
                item[c] = r.get("flow_tape_regime", r.get("market_regime"))
            else:
                item[c] = r.get(c)
        item["synced_at"] = r.get("synced_at") or synced_at
        payload.append(item)
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_flow_event_legs(
    conn: sqlite3.Connection,
    *,
    flow_version: str,
    event_dates: list[str] | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM flow_event_legs WHERE flow_version = ?"
    params: list[object] = [flow_version]
    if event_dates:
        placeholders = ",".join("?" * len(event_dates))
        sql += f" AND event_date IN ({placeholders})"
        params.extend(event_dates)
    sql += " ORDER BY event_date DESC, etf_id, stock_id"
    return conn.execute(sql, params).fetchall()
