"""VCP screen scores and Qlib TW factor storage."""
from __future__ import annotations

import sqlite3

from stock_db.util import utc_now_iso

def upsert_vcp_screen_scores_v2(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO vcp_screen_scores_v2 (
            stock_id, as_of_date, model_id, stock_name, composite_score, rating,
            execution_state, entry_ready, pattern_type, pivot_price,
            distance_from_pivot_pct, stop_loss, risk_pct, valid_vcp, metadata_json,
            synced_at
        ) VALUES (
            :stock_id, :as_of_date, :model_id, :stock_name, :composite_score, :rating,
            :execution_state, :entry_ready, :pattern_type, :pivot_price,
            :distance_from_pivot_pct, :stop_loss, :risk_pct, :valid_vcp, :metadata_json,
            :synced_at
        )
        ON CONFLICT(stock_id, as_of_date, model_id) DO UPDATE SET
            stock_name=excluded.stock_name,
            composite_score=excluded.composite_score,
            rating=excluded.rating,
            execution_state=excluded.execution_state,
            entry_ready=excluded.entry_ready,
            pattern_type=excluded.pattern_type,
            pivot_price=excluded.pivot_price,
            distance_from_pivot_pct=excluded.distance_from_pivot_pct,
            stop_loss=excluded.stop_loss,
            risk_pct=excluded.risk_pct,
            valid_vcp=excluded.valid_vcp,
            metadata_json=excluded.metadata_json,
            synced_at=excluded.synced_at
    """
    payload = []
    for r in rows:
        payload.append(
            {
                "stock_id": r["stock_id"],
                "as_of_date": r["as_of_date"],
                "model_id": r["model_id"],
                "stock_name": r.get("stock_name"),
                "composite_score": float(r["composite_score"]),
                "rating": r["rating"],
                "execution_state": r["execution_state"],
                "entry_ready": int(r.get("entry_ready") or 0),
                "pattern_type": r.get("pattern_type"),
                "pivot_price": r.get("pivot_price"),
                "distance_from_pivot_pct": r.get("distance_from_pivot_pct"),
                "stop_loss": r.get("stop_loss"),
                "risk_pct": r.get("risk_pct"),
                "valid_vcp": r.get("valid_vcp"),
                "metadata_json": r.get("metadata_json"),
                "synced_at": r.get("synced_at") or synced_at,
            }
        )
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def delete_vcp_screen_scores_v2_for_date(
    conn: sqlite3.Connection,
    as_of_date: str,
    *,
    model_id: str,
) -> int:
    cur = conn.execute(
        """
        DELETE FROM vcp_screen_scores_v2
        WHERE as_of_date = ? AND model_id = ?
        """,
        (as_of_date, model_id),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def delete_vcp_screen_scores_v2_for_model(
    conn: sqlite3.Connection,
    model_id: str,
) -> int:
    cur = conn.execute(
        "DELETE FROM vcp_screen_scores_v2 WHERE model_id = ?",
        (model_id,),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def load_vcp_screen_dates_for_model(
    conn: sqlite3.Connection,
    *,
    model_id: str,
    date_start: str | None = None,
    date_end: str | None = None,
) -> list[str]:
    sql = """
        SELECT DISTINCT as_of_date AS d
        FROM vcp_screen_scores_v2
        WHERE model_id = ?
    """
    params: list[object] = [model_id]
    if date_start:
        sql += " AND as_of_date >= ?"
        params.append(date_start)
    if date_end:
        sql += " AND as_of_date <= ?"
        params.append(date_end)
    sql += " ORDER BY d ASC"
    return [str(r["d"]) for r in conn.execute(sql, params).fetchall()]


def load_vcp_screen_v2_for_date(
    conn: sqlite3.Connection,
    as_of_date: str,
    *,
    model_id: str,
    min_score: float = 0.0,
    execution_states: tuple[str, ...] | list[str] | None = None,
) -> list[sqlite3.Row]:
    sql = """
        SELECT * FROM vcp_screen_scores_v2
        WHERE as_of_date = ? AND model_id = ? AND composite_score >= ?
    """
    params: list[object] = [as_of_date, model_id, min_score]
    if execution_states:
        placeholders = ",".join("?" * len(execution_states))
        sql += f" AND execution_state IN ({placeholders})"
        params.extend(execution_states)
    sql += " ORDER BY composite_score DESC, stock_id ASC"
    return conn.execute(sql, params).fetchall()
def upsert_qlib_tw_factor_scores(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO qlib_tw_factor_scores (
            stock_id, as_of_date, model_id, stock_name, composite_score,
            rank_n, feature_date, features_json, synced_at
        ) VALUES (
            :stock_id, :as_of_date, :model_id, :stock_name, :composite_score,
            :rank_n, :feature_date, :features_json, :synced_at
        )
        ON CONFLICT(stock_id, as_of_date, model_id) DO UPDATE SET
            stock_name=excluded.stock_name,
            composite_score=excluded.composite_score,
            rank_n=excluded.rank_n,
            feature_date=excluded.feature_date,
            features_json=excluded.features_json,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": r.get("synced_at") or synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_qlib_tw_factor_for_date(
    conn: sqlite3.Connection,
    as_of_date: str,
    *,
    model_id: str = "qlib-tw-factor",
    top_k: int | None = None,
) -> list[sqlite3.Row]:
    sql = """
        SELECT * FROM qlib_tw_factor_scores
        WHERE as_of_date = ? AND model_id = ?
        ORDER BY rank_n ASC, stock_id ASC
    """
    params: list[object] = [as_of_date, model_id]
    if top_k is not None:
        sql += " LIMIT ?"
        params.append(int(top_k))
    return conn.execute(sql, params).fetchall()


def list_qlib_tw_factor_dates(
    conn: sqlite3.Connection,
    *,
    model_id: str,
    as_of: str,
    lookback: int,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT as_of_date AS d
        FROM qlib_tw_factor_scores
        WHERE model_id = ? AND as_of_date <= ?
        ORDER BY d DESC
        LIMIT ?
        """,
        (model_id, as_of, lookback),
    ).fetchall()
    return sorted(str(r["d"]) for r in rows)


def load_latest_qlib_tw_factor_date(
    conn: sqlite3.Connection,
    *,
    model_id: str,
    as_of: str,
) -> str | None:
    row = conn.execute(
        """
        SELECT MAX(as_of_date) AS d
        FROM qlib_tw_factor_scores
        WHERE model_id = ? AND as_of_date <= ?
        """,
        (model_id, as_of),
    ).fetchone()
    return str(row["d"]) if row and row["d"] else None


def load_latest_vcp_screen_v2_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(as_of_date) AS d FROM vcp_screen_scores_v2").fetchone()
    return str(row["d"]) if row and row["d"] else None
