"""Stock fundamentals, catalysts, scores, PM watchlist."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from stock_db.util import utc_now_iso

def upsert_stock_beta(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_beta (
            stock_id, name, market, beta, beta_window, benchmark, source, as_of_date, synced_at
        ) VALUES (
            :stock_id, :name, :market, :beta, :beta_window, :benchmark, :source, :as_of_date, :synced_at
        )
        ON CONFLICT(stock_id, source, beta_window) DO UPDATE SET
            name=excluded.name,
            market=excluded.market,
            beta=excluded.beta,
            benchmark=excluded.benchmark,
            as_of_date=excluded.as_of_date,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_stock_beta_map(
    conn: sqlite3.Connection,
) -> tuple[dict[str, sqlite3.Row], str | None]:
    """Latest stock_beta rows keyed by stock_id. Empty if table missing or empty."""
    try:
        as_of_row = conn.execute(
            "SELECT MAX(as_of_date) AS d FROM stock_beta"
        ).fetchone()
    except sqlite3.OperationalError:
        return {}, None
    if as_of_row is None or as_of_row["d"] is None:
        return {}, None
    as_of_date = as_of_row["d"]
    rows = conn.execute(
        """
        SELECT stock_id, beta, beta_window, source, as_of_date
        FROM stock_beta
        WHERE as_of_date = ?
        """,
        (as_of_date,),
    ).fetchall()
    return {row["stock_id"]: row for row in rows}, as_of_date


def load_stock_market_map(
    conn: sqlite3.Connection,
    stock_ids: list[str] | None = None,
) -> dict[str, str]:
    """Latest stock_beta.market per stock_id（TSE|OTC）。"""
    try:
        as_of_row = conn.execute(
            "SELECT MAX(as_of_date) AS d FROM stock_beta"
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if as_of_row is None or as_of_row["d"] is None:
        return {}
    as_of_date = as_of_row["d"]
    if stock_ids:
        placeholders = ",".join("?" * len(stock_ids))
        rows = conn.execute(
            f"""
            SELECT stock_id, market FROM stock_beta
            WHERE as_of_date = ? AND stock_id IN ({placeholders})
            """,
            (as_of_date, *stock_ids),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT stock_id, market FROM stock_beta WHERE as_of_date = ?",
            (as_of_date,),
        ).fetchall()
    return {row["stock_id"]: row["market"] for row in rows}


def upsert_stock_fundamental(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_fundamental (
            stock_id, as_of_date, pe, pb, roe_ttm, eps_ttm,
            eps_latest_q, roe_latest_q, dividend_yield,
            revenue_yoy_pct, revenue_mom_accel_pp, source, synced_at
        ) VALUES (
            :stock_id, :as_of_date, :pe, :pb, :roe_ttm, :eps_ttm,
            :eps_latest_q, :roe_latest_q, :dividend_yield,
            :revenue_yoy_pct, :revenue_mom_accel_pp, :source, :synced_at
        )
        ON CONFLICT(stock_id, as_of_date, source) DO UPDATE SET
            pe=excluded.pe, pb=excluded.pb, roe_ttm=excluded.roe_ttm,
            eps_ttm=excluded.eps_ttm,
            eps_latest_q=excluded.eps_latest_q, roe_latest_q=excluded.roe_latest_q,
            dividend_yield=excluded.dividend_yield,
            revenue_yoy_pct=excluded.revenue_yoy_pct,
            revenue_mom_accel_pp=excluded.revenue_mom_accel_pp,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_consensus(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_consensus (
            stock_id, as_of_date, metric, consensus_value, source, synced_at
        ) VALUES (
            :stock_id, :as_of_date, :metric, :consensus_value, :source, :synced_at
        )
        ON CONFLICT(stock_id, as_of_date, metric, source) DO UPDATE SET
            consensus_value=excluded.consensus_value,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_financial_history(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_financial_history (
            stock_id, period_date, period_type, metric, value, source, synced_at
        ) VALUES (
            :stock_id, :period_date, :period_type, :metric, :value, :source, :synced_at
        )
        ON CONFLICT(stock_id, period_date, period_type, metric, source) DO UPDATE SET
            value=excluded.value,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_latest_fundamental_map(
    conn: sqlite3.Connection,
    stock_ids: list[str] | None = None,
) -> dict[str, sqlite3.Row]:
    try:
        if stock_ids:
            placeholders = ",".join("?" * len(stock_ids))
            rows = conn.execute(
                f"""
                SELECT f.* FROM stock_fundamental f
                INNER JOIN (
                    SELECT stock_id, MAX(as_of_date) AS d
                    FROM stock_fundamental
                    WHERE stock_id IN ({placeholders})
                    GROUP BY stock_id
                ) latest ON f.stock_id = latest.stock_id AND f.as_of_date = latest.d
                """,
                stock_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT f.* FROM stock_fundamental f
                INNER JOIN (
                    SELECT stock_id, MAX(as_of_date) AS d
                    FROM stock_fundamental
                    GROUP BY stock_id
                ) latest ON f.stock_id = latest.stock_id AND f.as_of_date = latest.d
                """
            ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["stock_id"]: row for row in rows}


def load_fundamental_map_as_of(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    as_of_date: str,
) -> dict[str, sqlite3.Row]:
    """各股在 as_of_date（含）以前最新基本面截面。"""
    if not stock_ids:
        return {}
    try:
        placeholders = ",".join("?" * len(stock_ids))
        rows = conn.execute(
            f"""
            SELECT f.* FROM stock_fundamental f
            INNER JOIN (
                SELECT stock_id, MAX(as_of_date) AS d
                FROM stock_fundamental
                WHERE stock_id IN ({placeholders}) AND as_of_date <= ?
                GROUP BY stock_id
            ) latest ON f.stock_id = latest.stock_id AND f.as_of_date = latest.d
            """,
            [*stock_ids, as_of_date],
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["stock_id"]: row for row in rows}


def load_latest_consensus_map(
    conn: sqlite3.Connection,
    stock_ids: list[str] | None = None,
) -> dict[str, dict[str, float]]:
    """stock_id -> {metric: consensus_value}（各 metric 取最新 as_of_date）。"""
    try:
        if stock_ids:
            placeholders = ",".join("?" * len(stock_ids))
            rows = conn.execute(
                f"""
                SELECT c.stock_id, c.metric, c.consensus_value
                FROM stock_consensus c
                INNER JOIN (
                    SELECT stock_id, metric, MAX(as_of_date) AS d
                    FROM stock_consensus
                    WHERE stock_id IN ({placeholders})
                    GROUP BY stock_id, metric
                ) latest
                    ON c.stock_id = latest.stock_id
                    AND c.metric = latest.metric
                    AND c.as_of_date = latest.d
                """,
                stock_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT c.stock_id, c.metric, c.consensus_value
                FROM stock_consensus c
                INNER JOIN (
                    SELECT stock_id, metric, MAX(as_of_date) AS d
                    FROM stock_consensus
                    GROUP BY stock_id, metric
                ) latest
                    ON c.stock_id = latest.stock_id
                    AND c.metric = latest.metric
                    AND c.as_of_date = latest.d
                """
            ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        out.setdefault(row["stock_id"], {})[row["metric"]] = float(row["consensus_value"])
    return out


def upsert_catalyst_events(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    ingested_at = utc_now_iso()
    sql = """
        INSERT INTO catalyst_events (
            event_id, stock_id, event_date, catalyst_type, headline,
            polarity, explains_etf_add, confidence, sources_json,
            source, ingested_at
        ) VALUES (
            :event_id, :stock_id, :event_date, :catalyst_type, :headline,
            :polarity, :explains_etf_add, :confidence, :sources_json,
            :source, :ingested_at
        )
        ON CONFLICT(event_id) DO UPDATE SET
            polarity=excluded.polarity,
            explains_etf_add=excluded.explains_etf_add,
            confidence=excluded.confidence,
            sources_json=excluded.sources_json,
            source=excluded.source,
            ingested_at=excluded.ingested_at
    """
    payload = [{**r, "ingested_at": ingested_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_catalyst_events(
    conn: sqlite3.Connection,
    *,
    stock_ids: list[str] | None = None,
    window_days: int = 7,
    as_of: str | None = None,
) -> list[sqlite3.Row]:
    try:
        ref = as_of or date.today().isoformat()
        start = (
            datetime.fromisoformat(ref).date()
            - timedelta(days=window_days)
        ).isoformat()
        if stock_ids:
            placeholders = ",".join("?" * len(stock_ids))
            return conn.execute(
                f"""
                SELECT * FROM catalyst_events
                WHERE event_date >= ? AND event_date <= ?
                  AND stock_id IN ({placeholders})
                ORDER BY event_date DESC, confidence DESC
                """,
                [start, ref, *stock_ids],
            ).fetchall()
        return conn.execute(
            """
            SELECT * FROM catalyst_events
            WHERE event_date >= ? AND event_date <= ?
            ORDER BY event_date DESC, confidence DESC
            """,
            (start, ref),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def load_memo_candidates(
    conn: sqlite3.Connection,
    *,
    as_of_date: str | None = None,
    top_n: int = 10,
    watchlist: str = "首要觀察",
) -> list[sqlite3.Row]:
    """觀察名單首要觀察 TopN；若無則改取同 as_of 綜合評分最高（供備忘草稿）。"""
    try:
        if as_of_date is None:
            row = conn.execute(
                "SELECT MAX(as_of_date) AS d FROM investment_scores"
            ).fetchone()
            if row is None or row["d"] is None:
                return []
            as_of_date = row["d"]
        rows = conn.execute(
            """
            SELECT * FROM investment_scores
            WHERE as_of_date = ? AND watchlist IN (?, 'A')
            ORDER BY investment_score DESC
            LIMIT ?
            """,
            (as_of_date, watchlist, top_n),
        ).fetchall()
        if rows:
            return rows
        return conn.execute(
            """
            SELECT * FROM investment_scores
            WHERE as_of_date = ?
            ORDER BY investment_score DESC
            LIMIT ?
            """,
            (as_of_date, top_n),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def upsert_research_memos(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO research_memos (
            memo_date, stock_id, rank, watchlist, investment_score,
            body_md, context_json, llm_used, audit_passed, audit_notes, synced_at
        ) VALUES (
            :memo_date, :stock_id, :rank, :watchlist, :investment_score,
            :body_md, :context_json, :llm_used, :audit_passed, :audit_notes, :synced_at
        )
        ON CONFLICT(memo_date, stock_id) DO UPDATE SET
            rank=excluded.rank,
            watchlist=excluded.watchlist,
            investment_score=excluded.investment_score,
            body_md=excluded.body_md,
            context_json=excluded.context_json,
            llm_used=excluded.llm_used,
            audit_passed=excluded.audit_passed,
            audit_notes=excluded.audit_notes,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)
def upsert_investment_scores(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO investment_scores (
            stock_id, as_of_date, score_version, stock_name,
            smart_money, catalyst, expectation, fundamental, risk,
            investment_score, watchlist,
            pool_reason, money_rank, event_rank, position_intent,
            tech_risk_flag, metadata_json, synced_at
        ) VALUES (
            :stock_id, :as_of_date, :score_version, :stock_name,
            :smart_money, :catalyst, :expectation, :fundamental, :risk,
            :investment_score, :watchlist,
            :pool_reason, :money_rank, :event_rank, :position_intent,
            :tech_risk_flag, :metadata_json, :synced_at
        )
        ON CONFLICT(stock_id, as_of_date, score_version) DO UPDATE SET
            stock_name=excluded.stock_name,
            smart_money=excluded.smart_money,
            catalyst=excluded.catalyst,
            expectation=excluded.expectation,
            fundamental=excluded.fundamental,
            risk=excluded.risk,
            investment_score=excluded.investment_score,
            watchlist=excluded.watchlist,
            pool_reason=excluded.pool_reason,
            money_rank=excluded.money_rank,
            event_rank=excluded.event_rank,
            position_intent=excluded.position_intent,
            tech_risk_flag=excluded.tech_risk_flag,
            metadata_json=excluded.metadata_json,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_pm_watchlist(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO pm_watchlist (
            stock_id, as_of_date, score_version, stock_name,
            investment_score, watchlist, entry_signal, entry_tags_json, chip_tag,
            pm_bucket,
            flow_score, chip_score, tech_score, catalyst_score, fundamental_score,
            note, synced_at
        ) VALUES (
            :stock_id, :as_of_date, :score_version, :stock_name,
            :investment_score, :watchlist, :entry_signal, :entry_tags_json, :chip_tag,
            :pm_bucket,
            :flow_score, :chip_score, :tech_score, :catalyst_score, :fundamental_score,
            :note, :synced_at
        )
        ON CONFLICT(stock_id, as_of_date, score_version) DO UPDATE SET
            stock_name=excluded.stock_name,
            investment_score=excluded.investment_score,
            watchlist=excluded.watchlist,
            entry_signal=excluded.entry_signal,
            entry_tags_json=excluded.entry_tags_json,
            chip_tag=excluded.chip_tag,
            pm_bucket=excluded.pm_bucket,
            flow_score=excluded.flow_score,
            chip_score=excluded.chip_score,
            tech_score=excluded.tech_score,
            catalyst_score=excluded.catalyst_score,
            fundamental_score=excluded.fundamental_score,
            note=excluded.note,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_latest_pm_watchlist(
    conn: sqlite3.Connection,
    *,
    score_version: str | None = None,
) -> list[sqlite3.Row]:
    version = score_version or "p4-v2"
    try:
        row = conn.execute(
            """
            SELECT MAX(as_of_date) AS d
            FROM pm_watchlist
            WHERE score_version = ?
            """,
            (version,),
        ).fetchone()
    except sqlite3.OperationalError:
        return []
    if row is None or row["d"] is None:
        return []
    return conn.execute(
        """
        SELECT *
        FROM pm_watchlist
        WHERE as_of_date = ? AND score_version = ?
        ORDER BY
            CASE pm_bucket
                WHEN '突破' THEN 0
                WHEN '觀察' THEN 1
                WHEN 'BREAKOUT' THEN 0
                WHEN 'RESEARCH' THEN 1
                ELSE 2
            END,
            investment_score DESC,
            stock_id
        """,
        (row["d"], version),
    ).fetchall()

