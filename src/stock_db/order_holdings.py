"""Order layer · holdings snapshot SSOT for morning brief / sell-radar."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OrderHoldingSnapshotRow:
    snapshot_date: str
    stock_id: str
    stock_name: str
    shares: int
    avg_cost: float | None
    unrealized_pnl: int
    prev_close: float | None
    bar_date: str | None
    rrg_quadrant: str | None
    rrg_session: str | None
    structure_tier: str | None
    gate_2pct: float | None
    trigger_s1b: float | None
    extension_spike: float | None
    notes: tuple[str, ...]
    source: str
    synced_at: str


def _row_from_dict(snapshot_date: str, item: dict[str, Any], *, synced_at: str, source: str) -> OrderHoldingSnapshotRow:
    notes_raw = item.get("notes")
    if isinstance(notes_raw, (list, tuple)):
        notes = tuple(str(n) for n in notes_raw)
    else:
        notes = ()
    return OrderHoldingSnapshotRow(
        snapshot_date=snapshot_date,
        stock_id=str(item["stock_id"]),
        stock_name=str(item.get("stock_name") or ""),
        shares=int(item.get("shares") or 0),
        avg_cost=float(item["avg_cost"]) if item.get("avg_cost") is not None else None,
        unrealized_pnl=int(item.get("unrealized_pnl") or 0),
        prev_close=float(item["prev_close"]) if item.get("prev_close") is not None else None,
        bar_date=str(item["bar_date"]) if item.get("bar_date") else None,
        rrg_quadrant=str(item["rrg_quadrant"]) if item.get("rrg_quadrant") else None,
        rrg_session=str(item["rrg_session"]) if item.get("rrg_session") else None,
        structure_tier=str(item["structure_tier"]) if item.get("structure_tier") else None,
        gate_2pct=float(item["gate_2pct"]) if item.get("gate_2pct") is not None else None,
        trigger_s1b=float(item["trigger_s1b"]) if item.get("trigger_s1b") is not None else None,
        extension_spike=float(item["extension_spike"]) if item.get("extension_spike") is not None else None,
        notes=notes,
        source=source,
        synced_at=synced_at,
    )


def upsert_order_holdings_snapshot(
    conn: sqlite3.Connection,
    rows: list[OrderHoldingSnapshotRow],
) -> int:
    if not rows:
        return 0
    snapshot_date = rows[0].snapshot_date
    conn.execute("DELETE FROM order_holdings_snapshot WHERE snapshot_date = ?", (snapshot_date,))
    conn.executemany(
        """
        INSERT INTO order_holdings_snapshot (
            snapshot_date, stock_id, stock_name, shares, avg_cost, unrealized_pnl,
            prev_close, bar_date, rrg_quadrant, rrg_session, structure_tier,
            gate_2pct, trigger_s1b, extension_spike, notes_json, source, synced_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r.snapshot_date,
                r.stock_id,
                r.stock_name,
                r.shares,
                r.avg_cost,
                r.unrealized_pnl,
                r.prev_close,
                r.bar_date,
                r.rrg_quadrant,
                r.rrg_session,
                r.structure_tier,
                r.gate_2pct,
                r.trigger_s1b,
                r.extension_spike,
                json.dumps(list(r.notes), ensure_ascii=False),
                r.source,
                r.synced_at,
            )
            for r in rows
        ],
    )
    conn.commit()
    return len(rows)


def load_order_holdings_snapshot(
    conn: sqlite3.Connection,
    snapshot_date: str,
) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT stock_id, shares FROM order_holdings_snapshot
        WHERE snapshot_date = ? AND shares > 0
        ORDER BY stock_id
        """,
        (snapshot_date,),
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def load_latest_order_holdings_snapshot(
    conn: sqlite3.Connection,
    *,
    on_or_before: str | None = None,
) -> dict[str, int]:
    if on_or_before:
        row = conn.execute(
            """
            SELECT snapshot_date FROM order_holdings_snapshot
            WHERE snapshot_date <= ?
            ORDER BY snapshot_date DESC LIMIT 1
            """,
            (on_or_before,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT snapshot_date FROM order_holdings_snapshot ORDER BY snapshot_date DESC LIMIT 1"
        ).fetchone()
    if not row:
        return {}
    return load_order_holdings_snapshot(conn, str(row[0]))


def load_order_holdings_snapshot_rows(
    conn: sqlite3.Connection,
    snapshot_date: str,
) -> list[OrderHoldingSnapshotRow]:
    cur = conn.execute(
        """
        SELECT snapshot_date, stock_id, stock_name, shares, avg_cost, unrealized_pnl,
               prev_close, bar_date, rrg_quadrant, rrg_session, structure_tier,
               gate_2pct, trigger_s1b, extension_spike, notes_json, source, synced_at
        FROM order_holdings_snapshot
        WHERE snapshot_date = ?
        ORDER BY structure_tier = 'weak' DESC, unrealized_pnl ASC, stock_id
        """,
        (snapshot_date,),
    )
    out: list[OrderHoldingSnapshotRow] = []
    for row in cur.fetchall():
        notes: tuple[str, ...] = ()
        if row[14]:
            try:
                parsed = json.loads(row[14])
                if isinstance(parsed, list):
                    notes = tuple(str(n) for n in parsed)
            except json.JSONDecodeError:
                notes = ()
        out.append(
            OrderHoldingSnapshotRow(
                snapshot_date=str(row[0]),
                stock_id=str(row[1]),
                stock_name=str(row[2] or ""),
                shares=int(row[3]),
                avg_cost=float(row[4]) if row[4] is not None else None,
                unrealized_pnl=int(row[5] or 0),
                prev_close=float(row[6]) if row[6] is not None else None,
                bar_date=str(row[7]) if row[7] else None,
                rrg_quadrant=str(row[8]) if row[8] else None,
                rrg_session=str(row[9]) if row[9] else None,
                structure_tier=str(row[10]) if row[10] else None,
                gate_2pct=float(row[11]) if row[11] is not None else None,
                trigger_s1b=float(row[12]) if row[12] is not None else None,
                extension_spike=float(row[13]) if row[13] is not None else None,
                notes=notes,
                source=str(row[15] or "fubon"),
                synced_at=str(row[16]),
            )
        )
    return out


def append_intraday_exit_log(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    checked_at: str,
    phase: str,
    event: str,
    stock_id: str | None = None,
    detail: dict[str, Any] | None = None,
    dry_run: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO order_intraday_exit_log (
            trade_date, checked_at, phase, stock_id, event, detail_json, dry_run
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trade_date,
            checked_at,
            phase,
            stock_id,
            event,
            json.dumps(detail or {}, ensure_ascii=False),
            1 if dry_run else 0,
        ),
    )
    conn.commit()
