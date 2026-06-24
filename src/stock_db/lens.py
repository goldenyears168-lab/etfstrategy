"""Lens SQLite persistence · alert + local highlight cache（回測 PIT）。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from stock_db.util import utc_now_iso


def upsert_lens_daily_alert(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    synced_at = utc_now_iso()
    items = row.get("items_json")
    if isinstance(items, (list, dict)):
        items_json = json.dumps(items, ensure_ascii=False)
    else:
        items_json = str(items or "[]")
    conn.execute(
        """
        INSERT INTO lens_daily_alert (
            trade_date, total_count, fire_count, delta_new_count, consensus_add_count,
            headline_zh, items_json, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date) DO UPDATE SET
            total_count=excluded.total_count,
            fire_count=excluded.fire_count,
            delta_new_count=excluded.delta_new_count,
            consensus_add_count=excluded.consensus_add_count,
            headline_zh=excluded.headline_zh,
            items_json=excluded.items_json,
            computed_at=excluded.computed_at
        """,
        (
            row["trade_date"],
            int(row.get("total_count") or 0),
            int(row.get("fire_count") or 0),
            int(row.get("delta_new_count") or 0),
            int(row.get("consensus_add_count") or 0),
            row["headline_zh"],
            items_json,
            row.get("computed_at") or synced_at,
        ),
    )
    conn.commit()


def load_lens_daily_alert(
    conn: sqlite3.Connection,
    trade_date: str,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM lens_daily_alert WHERE trade_date = ?",
        (trade_date,),
    ).fetchone()


def _highlight_row_for_storage(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    codes = payload.get("etf_add_codes")
    if isinstance(codes, list):
        payload["etf_add_codes"] = list(codes)
    src = payload.get("sources_json")
    if isinstance(src, dict):
        payload["sources_json"] = dict(src)
    return payload


def upsert_lens_daily_highlight(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO lens_daily_highlight (
            trade_date, stock_id, row_json, lens_score, highlight_tier,
            rrg_quadrant, rrg_mono_fresh, rrg_tier2, synced_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, stock_id) DO UPDATE SET
            row_json=excluded.row_json,
            lens_score=excluded.lens_score,
            highlight_tier=excluded.highlight_tier,
            rrg_quadrant=excluded.rrg_quadrant,
            rrg_mono_fresh=excluded.rrg_mono_fresh,
            rrg_tier2=excluded.rrg_tier2,
            synced_at=excluded.synced_at
    """
    payload = []
    for row in rows:
        stored = _highlight_row_for_storage(row)
        payload.append(
            (
                stored["trade_date"],
                stored["stock_id"],
                json.dumps(stored, ensure_ascii=False),
                float(stored.get("lens_score") or 0),
                str(stored.get("highlight_tier") or "none"),
                stored.get("rrg_quadrant"),
                1 if stored.get("rrg_mono_fresh") else 0,
                1 if stored.get("rrg_tier2") else 0,
                synced_at,
            )
        )
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_lens_daily_highlight(
    conn: sqlite3.Connection,
    trade_date: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT row_json FROM lens_daily_highlight WHERE trade_date = ? ORDER BY stock_id",
        (trade_date,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            out.append(json.loads(str(row["row_json"])))
        except json.JSONDecodeError:
            continue
    return out


def load_lens_daily_highlight_stock_ids(
    conn: sqlite3.Connection,
    trade_date: str,
) -> set[str]:
    rows = conn.execute(
        "SELECT stock_id FROM lens_daily_highlight WHERE trade_date = ?",
        (trade_date,),
    ).fetchall()
    return {str(r["stock_id"]) for r in rows}


def count_lens_daily_highlight_dates(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
) -> int:
    clauses: list[str] = []
    params: list[str] = []
    if start:
        clauses.append("trade_date >= ?")
        params.append(start)
    if end:
        clauses.append("trade_date <= ?")
        params.append(end)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    row = conn.execute(
        f"SELECT COUNT(DISTINCT trade_date) AS n FROM lens_daily_highlight{where}",
        params,
    ).fetchone()
    return int(row["n"] or 0) if row else 0
