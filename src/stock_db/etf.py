"""ETF/mutual-fund holdings, signals, behavior, watchlist universe."""
from __future__ import annotations

import sqlite3

from stock_db.util import utc_now_iso

def upsert_mutual_fund_holdings_meta(conn: sqlite3.Connection, row: dict) -> None:
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO mutual_fund_holdings_meta (
            fund_code, snapshot_date, fund_name, disclosure_type, fund_size_billion,
            holding_count, source, source_edit_at, synced_at
        ) VALUES (
            :fund_code, :snapshot_date, :fund_name, :disclosure_type, :fund_size_billion,
            :holding_count, :source, :source_edit_at, :synced_at
        )
        ON CONFLICT(fund_code, snapshot_date, disclosure_type) DO UPDATE SET
            fund_name=excluded.fund_name,
            fund_size_billion=excluded.fund_size_billion,
            holding_count=excluded.holding_count,
            source=excluded.source,
            source_edit_at=excluded.source_edit_at,
            synced_at=excluded.synced_at
    """
    conn.execute(sql, {**row, "synced_at": synced_at})
    conn.commit()


def upsert_mutual_fund_holdings(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO mutual_fund_holdings (
            fund_code, snapshot_date, disclosure_type, stock_id, stock_name, rank_no,
            shares, weight_pct, amount, asset_type, source, source_edit_at, synced_at
        ) VALUES (
            :fund_code, :snapshot_date, :disclosure_type, :stock_id, :stock_name, :rank_no,
            :shares, :weight_pct, :amount, :asset_type, :source, :source_edit_at, :synced_at
        )
        ON CONFLICT(fund_code, snapshot_date, disclosure_type, stock_id) DO UPDATE SET
            stock_name=excluded.stock_name,
            rank_no=excluded.rank_no,
            shares=excluded.shares,
            weight_pct=excluded.weight_pct,
            amount=excluded.amount,
            asset_type=excluded.asset_type,
            source=excluded.source,
            source_edit_at=excluded.source_edit_at,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def list_mutual_fund_snapshot_dates(
    conn: sqlite3.Connection,
    fund_code: str,
    *,
    disclosure_type: str | None = None,
) -> list[str]:
    sql = """
        SELECT DISTINCT snapshot_date
        FROM mutual_fund_holdings_meta
        WHERE fund_code = ?
    """
    params: list[str] = [fund_code]
    if disclosure_type:
        sql += " AND disclosure_type = ?"
        params.append(disclosure_type)
    sql += " ORDER BY snapshot_date DESC"
    return [row[0] for row in conn.execute(sql, params)]


def load_mutual_fund_holdings(
    conn: sqlite3.Connection,
    fund_code: str,
    snapshot_date: str,
    *,
    disclosure_type: str | None = None,
) -> list[sqlite3.Row]:
    sql = """
        SELECT fund_code, snapshot_date, disclosure_type, stock_id, stock_name, rank_no,
               shares, weight_pct, amount, asset_type, source, source_edit_at, synced_at
        FROM mutual_fund_holdings
        WHERE fund_code = ? AND snapshot_date = ?
    """
    params: list[str] = [fund_code, snapshot_date]
    if disclosure_type:
        sql += " AND disclosure_type = ?"
        params.append(disclosure_type)
    sql += " ORDER BY rank_no IS NULL, rank_no, weight_pct DESC, stock_id"
    return list(conn.execute(sql, params))


def upsert_etf_holdings_meta(conn: sqlite3.Connection, row: dict) -> None:
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO etf_holdings_meta (
            etf_code, snapshot_date, nav, holding_count, source, source_edit_at, synced_at
        ) VALUES (
            :etf_code, :snapshot_date, :nav, :holding_count, :source, :source_edit_at, :synced_at
        )
        ON CONFLICT(etf_code, snapshot_date) DO UPDATE SET
            nav=excluded.nav,
            holding_count=excluded.holding_count,
            source=excluded.source,
            source_edit_at=excluded.source_edit_at,
            synced_at=excluded.synced_at
    """
    conn.execute(sql, {**row, "synced_at": synced_at})
    conn.commit()


def upsert_etf_holdings(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO etf_holdings (
            etf_code, snapshot_date, stock_id, stock_name, shares, weight_pct, amount,
            source, source_edit_at, synced_at
        ) VALUES (
            :etf_code, :snapshot_date, :stock_id, :stock_name, :shares, :weight_pct, :amount,
            :source, :source_edit_at, :synced_at
        )
        ON CONFLICT(etf_code, snapshot_date, stock_id) DO UPDATE SET
            stock_name=excluded.stock_name,
            shares=excluded.shares,
            weight_pct=excluded.weight_pct,
            amount=excluded.amount,
            source=excluded.source,
            source_edit_at=excluded.source_edit_at,
            synced_at=excluded.synced_at
    """
    payload = [
        {
            **r,
            "stock_name": normalize_stock_name(r.get("stock_name")),
            "synced_at": synced_at,
        }
        for r in rows
    ]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def list_etf_snapshot_dates(conn: sqlite3.Connection, etf_code: str) -> list[str]:
    sql = """
        SELECT snapshot_date
        FROM etf_holdings_meta
        WHERE etf_code = ?
        ORDER BY snapshot_date DESC
    """
    return [row[0] for row in conn.execute(sql, (etf_code,))]


def load_etf_holdings_meta(
    conn: sqlite3.Connection,
    etf_code: str,
    snapshot_date: str,
) -> sqlite3.Row | None:
    sql = """
        SELECT etf_code, snapshot_date, nav, holding_count, source, source_edit_at, synced_at
        FROM etf_holdings_meta
        WHERE etf_code = ? AND snapshot_date = ?
    """
    row = conn.execute(sql, (etf_code, snapshot_date)).fetchone()
    return row


def load_etf_holdings(
    conn: sqlite3.Connection,
    etf_code: str,
    snapshot_date: str,
) -> list[sqlite3.Row]:
    sql = """
        SELECT etf_code, snapshot_date, stock_id, stock_name, shares, weight_pct, amount,
               source, source_edit_at, synced_at
        FROM etf_holdings
        WHERE etf_code = ? AND snapshot_date = ?
        ORDER BY stock_id
    """
    return list(conn.execute(sql, (etf_code, snapshot_date)))


def resolve_holdings_weight_pct(rows: list) -> dict[str, float]:
    """
    stock_id -> weight_pct on 0–100 scale.

    Never mix raw share counts with percentage weights (etfedge 缺 close 時
    weight_pct 為 NULL，舊邏輯用 shares 當 weight 會產生百萬級 inv_weight_pct)。
    """
    by_sid: dict[str, float] = {}
    shares: dict[str, float] = {}
    for row in rows:
        sh = float(row["shares"] or 0)
        if sh <= 0:
            continue
        sid = str(row["stock_id"])
        shares[sid] = sh
        w = row["weight_pct"]
        if w is not None:
            by_sid[sid] = float(w)

    if not shares:
        return by_sid

    missing = [sid for sid in shares if sid not in by_sid]
    if not missing:
        return by_sid

    total_sh = sum(shares.values())
    if total_sh <= 0:
        return by_sid

    for sid in missing:
        by_sid[sid] = 100.0 * shares[sid] / total_sh
    return by_sid


def backfill_etf_holdings_weight_pct(
    conn: sqlite3.Connection,
    etf_code: str | None = None,
) -> int:
    """補齊 etf_holdings.weight_pct（僅 UPDATE 仍為 NULL 的列）。"""
    sql = """
        SELECT DISTINCT etf_code, snapshot_date
        FROM etf_holdings
        WHERE weight_pct IS NULL
    """
    params: tuple = ()
    if etf_code:
        sql += " AND etf_code = ?"
        params = (etf_code,)
    pairs = conn.execute(sql, params).fetchall()
    updated = 0
    for row in pairs:
        code, snap = str(row[0]), str(row[1])
        holdings = load_etf_holdings(conn, code, snap)
        weights = resolve_holdings_weight_pct(holdings)
        for h in holdings:
            if h["weight_pct"] is not None:
                continue
            sid = str(h["stock_id"])
            w = weights.get(sid)
            if w is None:
                continue
            conn.execute(
                """
                UPDATE etf_holdings
                SET weight_pct = ?
                WHERE etf_code = ? AND snapshot_date = ? AND stock_id = ?
                """,
                (round(w, 6), code, snap, sid),
            )
            updated += 1
    if updated:
        conn.commit()
    return updated


def normalize_stock_name(name: str | None) -> str:
    """Repair legacy etf_holdings names double-encoded as UTF-8 mojibake."""
    text = (name or "").strip()
    if not text:
        return ""
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return text
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text
    if any("\u4e00" <= ch <= "\u9fff" for ch in repaired):
        return repaired
    return text


def repair_mojibake_stock_names_in_etf_holdings(conn: sqlite3.Connection) -> int:
    """Backfill etf_holdings.stock_name where legacy rows were stored as mojibake."""
    rows = conn.execute(
        """
        SELECT DISTINCT stock_id, stock_name
        FROM etf_holdings
        WHERE stock_name IS NOT NULL AND TRIM(stock_name) != ''
        """
    ).fetchall()
    updated = 0
    for row in rows:
        fixed = normalize_stock_name(row["stock_name"])
        if fixed == row["stock_name"]:
            continue
        cur = conn.execute(
            """
            UPDATE etf_holdings
            SET stock_name = ?
            WHERE stock_id = ? AND stock_name = ?
            """,
            (fixed, row["stock_id"], row["stock_name"]),
        )
        updated += cur.rowcount
    if updated:
        conn.commit()
    return updated


def compute_etf_holdings_changes(
    conn: sqlite3.Connection,
    etf_code: str,
    curr_date: str | None = None,
    prev_date: str | None = None,
) -> list[sqlite3.Row]:
    dates = list_etf_snapshot_dates(conn, etf_code)
    if not dates:
        return []
    if curr_date is None:
        curr_date = dates[0]
    if prev_date is None:
        if len(dates) < 2:
            return []
        prev_date = dates[1] if dates[0] == curr_date else dates[0]

    sql = """
        WITH curr AS (
            SELECT stock_id, stock_name, shares, weight_pct
            FROM etf_holdings
            WHERE etf_code = ? AND snapshot_date = ?
        ),
        prev AS (
            SELECT stock_id, stock_name, shares, weight_pct
            FROM etf_holdings
            WHERE etf_code = ? AND snapshot_date = ?
        )
        SELECT
            COALESCE(c.stock_id, p.stock_id) AS stock_id,
            COALESCE(c.stock_name, p.stock_name) AS stock_name,
            p.shares AS shares_prev,
            c.shares AS shares_curr,
            p.weight_pct AS weight_pct_prev,
            c.weight_pct AS weight_pct_curr,
            COALESCE(c.shares, 0) - COALESCE(p.shares, 0) AS share_delta,
            COALESCE(c.weight_pct, 0) - COALESCE(p.weight_pct, 0) AS weight_delta,
            CASE
                WHEN p.stock_id IS NULL THEN '新进'
                WHEN c.stock_id IS NULL THEN '出清'
                WHEN c.shares > p.shares THEN '加码'
                WHEN c.shares < p.shares THEN '减码'
                ELSE '不变'
            END AS action
        FROM curr c
        FULL OUTER JOIN prev p ON c.stock_id = p.stock_id
        ORDER BY stock_id
    """
    return list(conn.execute(sql, (etf_code, curr_date, etf_code, prev_date)))


def upsert_etf_daily_signal_snapshots(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO etf_daily_signal_snapshot (
            code, snapshot_date, close_price, foreign_net, investment_trust_net,
            dealer_self_net, three_institution_net, source, synced_at
        ) VALUES (
            :code, :snapshot_date, :close_price, :foreign_net, :investment_trust_net,
            :dealer_self_net, :three_institution_net, :source, :synced_at
        )
        ON CONFLICT(code, snapshot_date, source) DO UPDATE SET
            close_price=excluded.close_price,
            foreign_net=excluded.foreign_net,
            investment_trust_net=excluded.investment_trust_net,
            dealer_self_net=excluded.dealer_self_net,
            three_institution_net=excluded.three_institution_net,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)

ETF_CODES_INTRADAY_DEFAULT = (
    "00981A",
    "00403A",
    "009816",
    "00980A",
    "00982A",
    "00992A",
)

_MUTUAL_FUND_WATCHLIST_DISCLOSURES = ("monthly_top10", "quarterly_full")


def _load_mutual_fund_watchlist_stocks(
    conn: sqlite3.Connection,
    fund_codes: tuple[str, ...],
) -> dict[str, dict]:
    """各基金最新月前十大 + 季完整持股聯集（僅 fund 來源）。"""
    stocks: dict[str, dict] = {}
    for fund_code in fund_codes:
        for disclosure_type in _MUTUAL_FUND_WATCHLIST_DISCLOSURES:
            row = conn.execute(
                """
                SELECT snapshot_date
                FROM mutual_fund_holdings_meta
                WHERE fund_code = ? AND disclosure_type = ?
                ORDER BY snapshot_date DESC
                LIMIT 1
                """,
                (fund_code, disclosure_type),
            ).fetchone()
            if row is None:
                continue
            snapshot_date = row[0]
            for holding in load_mutual_fund_holdings(
                conn,
                fund_code,
                snapshot_date,
                disclosure_type=disclosure_type,
            ):
                stock_id = holding["stock_id"]
                if not stock_id:
                    continue
                entry = stocks.setdefault(
                    stock_id,
                    {
                        "stock_id": stock_id,
                        "stock_name": "",
                        "fund_hold_count": 0,
                        "_fund_codes": set(),
                    },
                )
                entry["_fund_codes"].add(fund_code)
                if holding["stock_name"]:
                    entry["stock_name"] = holding["stock_name"]
    for entry in stocks.values():
        entry["fund_hold_count"] = len(entry.pop("_fund_codes"))
    return stocks


def load_etf_constituent_watchlist(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...] = ETF_CODES_INTRADAY_DEFAULT,
    *,
    fund_codes: tuple[str, ...] | None = None,
    benchmark_codes: tuple[str, ...] | None = None,
) -> list[dict]:
    """最新各 ETF snapshot 持股聯集，並併入境內基金／基準 ETF 成分股（日內監控 universe）。"""
    from project_config import (
        BENCHMARK_ETF_WATCHLIST_CODES,
        MUTUAL_FUND_WATCHLIST_CODES,
        SUPPLEMENTAL_WATCHLIST_STOCKS,
    )
    from stock_db.benchmark import _load_benchmark_watchlist_stocks

    if fund_codes is None:
        fund_codes = MUTUAL_FUND_WATCHLIST_CODES
    if benchmark_codes is None:
        benchmark_codes = BENCHMARK_ETF_WATCHLIST_CODES

    by_id: dict[str, dict] = {}
    if etf_codes:
        placeholders = ",".join("?" * len(etf_codes))
        sql = f"""
            WITH latest AS (
                SELECT etf_code, MAX(snapshot_date) AS snapshot_date
                FROM etf_holdings_meta
                WHERE etf_code IN ({placeholders})
                GROUP BY etf_code
            )
            SELECT h.stock_id, MAX(h.stock_name) AS stock_name,
                   COUNT(DISTINCT h.etf_code) AS etf_hold_count
            FROM etf_holdings h
            INNER JOIN latest l
                ON h.etf_code = l.etf_code AND h.snapshot_date = l.snapshot_date
            WHERE h.shares > 0
            GROUP BY h.stock_id
        """
        for row in conn.execute(sql, etf_codes):
            by_id[row["stock_id"]] = {
                "stock_id": row["stock_id"],
                "stock_name": normalize_stock_name(row["stock_name"]),
                "etf_hold_count": int(row["etf_hold_count"]),
                "fund_hold_count": 0,
                "benchmark_hold_count": 0,
                "supplemental_hold_count": 0,
            }

    for stock_id, fund_row in _load_mutual_fund_watchlist_stocks(conn, fund_codes).items():
        if stock_id in by_id:
            by_id[stock_id]["fund_hold_count"] = fund_row["fund_hold_count"]
            if not by_id[stock_id]["stock_name"] and fund_row["stock_name"]:
                by_id[stock_id]["stock_name"] = fund_row["stock_name"]
        else:
            by_id[stock_id] = {
                "stock_id": stock_id,
                "stock_name": fund_row["stock_name"],
                "etf_hold_count": 0,
                "fund_hold_count": fund_row["fund_hold_count"],
                "benchmark_hold_count": 0,
                "supplemental_hold_count": 0,
            }

    for stock_id, bench_row in _load_benchmark_watchlist_stocks(conn, benchmark_codes).items():
        if stock_id in by_id:
            by_id[stock_id]["benchmark_hold_count"] = bench_row["benchmark_hold_count"]
            if not by_id[stock_id]["stock_name"] and bench_row["stock_name"]:
                by_id[stock_id]["stock_name"] = bench_row["stock_name"]
        else:
            by_id[stock_id] = {
                "stock_id": stock_id,
                "stock_name": bench_row["stock_name"],
                "etf_hold_count": 0,
                "fund_hold_count": 0,
                "benchmark_hold_count": bench_row["benchmark_hold_count"],
                "supplemental_hold_count": 0,
            }

    for stock_id, stock_name in SUPPLEMENTAL_WATCHLIST_STOCKS.items():
        if stock_id in by_id:
            if not by_id[stock_id]["stock_name"]:
                by_id[stock_id]["stock_name"] = stock_name
            continue
        by_id[stock_id] = {
            "stock_id": stock_id,
            "stock_name": stock_name,
            "etf_hold_count": 0,
            "fund_hold_count": 0,
            "benchmark_hold_count": 0,
            "supplemental_hold_count": 1,
        }

    return sorted(
        by_id.values(),
        key=lambda w: (
            -(
                w["etf_hold_count"]
                + w["fund_hold_count"]
                + w["benchmark_hold_count"]
                + w.get("supplemental_hold_count", 0)
            ),
            w["stock_id"],
        ),
    )


def load_etf_ever_held_constituents(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> list[dict]:
    """ETF 歷史曾持有（shares > 0）的成分股聯集。"""
    if not etf_codes:
        return []
    placeholders = ",".join("?" * len(etf_codes))
    sql = f"""
        SELECT h.stock_id,
               MAX(h.stock_name) AS stock_name,
               MIN(h.snapshot_date) AS first_seen,
               MAX(h.snapshot_date) AS last_seen,
               COUNT(DISTINCT h.etf_code) AS etf_hold_count
        FROM etf_holdings h
        WHERE h.etf_code IN ({placeholders}) AND h.shares > 0
        GROUP BY h.stock_id
        ORDER BY h.stock_id
    """
    return [dict(r) for r in conn.execute(sql, etf_codes)]


def load_etf_constituent_universe_gaps(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> list[dict]:
    """曾持有但不在最新 ETF snapshot 的成分股（backfill universe 缺口）。"""
    current = {
        w["stock_id"]
        for w in load_etf_constituent_watchlist(conn, etf_codes, fund_codes=())
    }
    gaps: list[dict] = []
    for row in load_etf_ever_held_constituents(conn, etf_codes):
        if row["stock_id"] in current:
            continue
        gaps.append(
            {
                "stock_id": row["stock_id"],
                "stock_name": row["stock_name"] or "",
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "etf_hold_count": int(row["etf_hold_count"]),
            }
        )
    return gaps
def upsert_etf_behavior_predictions(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO etf_behavior_predictions (
            etf_code, as_of_date, stock_id, model_id, model_version,
            score, rank_n, universe_n, features_json, synced_at
        ) VALUES (
            :etf_code, :as_of_date, :stock_id, :model_id, :model_version,
            :score, :rank_n, :universe_n, :features_json, :synced_at
        )
        ON CONFLICT(etf_code, as_of_date, stock_id, model_id) DO UPDATE SET
            model_version=excluded.model_version,
            score=excluded.score,
            rank_n=excluded.rank_n,
            universe_n=excluded.universe_n,
            features_json=excluded.features_json,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": r.get("synced_at") or synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_etf_behavior_validation(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO etf_behavior_validation (
            etf_code, score_date, outcome_date, model_id, model_version,
            add_cohort, eval_mode, k, n_universe, n_actual_adds,
            precision_at_k, recall_at_k, mean_rank_pct, median_rank_pct, ndcg_at_k,
            random_precision, lift_vs_random, top_k_json, hit_json, missed_json,
            synced_at
        ) VALUES (
            :etf_code, :score_date, :outcome_date, :model_id, :model_version,
            :add_cohort, :eval_mode, :k, :n_universe, :n_actual_adds,
            :precision_at_k, :recall_at_k, :mean_rank_pct, :median_rank_pct, :ndcg_at_k,
            :random_precision, :lift_vs_random, :top_k_json, :hit_json, :missed_json,
            :synced_at
        )
        ON CONFLICT(etf_code, score_date, outcome_date, model_id, add_cohort, eval_mode)
        DO UPDATE SET
            model_version=excluded.model_version,
            k=excluded.k,
            n_universe=excluded.n_universe,
            n_actual_adds=excluded.n_actual_adds,
            precision_at_k=excluded.precision_at_k,
            recall_at_k=excluded.recall_at_k,
            mean_rank_pct=excluded.mean_rank_pct,
            median_rank_pct=excluded.median_rank_pct,
            ndcg_at_k=excluded.ndcg_at_k,
            random_precision=excluded.random_precision,
            lift_vs_random=excluded.lift_vs_random,
            top_k_json=excluded.top_k_json,
            hit_json=excluded.hit_json,
            missed_json=excluded.missed_json,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": r.get("synced_at") or synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def insert_etf_holdings_fetch_log(conn: sqlite3.Connection, row: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO etf_holdings_fetch_log (
            etf_code, snapshot_date, source, fetched_at, source_edit_at,
            holding_count, nav, content_hash, raw_path, sync_status,
            prev_fetch_id, diff_summary, rows_added, rows_removed, rows_changed
        ) VALUES (
            :etf_code, :snapshot_date, :source, :fetched_at, :source_edit_at,
            :holding_count, :nav, :content_hash, :raw_path, :sync_status,
            :prev_fetch_id, :diff_summary, :rows_added, :rows_removed, :rows_changed
        )
        """,
        row,
    )
    conn.commit()
    return int(cur.lastrowid)


def load_latest_etf_holdings_fetch(
    conn: sqlite3.Connection,
    etf_code: str,
    snapshot_date: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM etf_holdings_fetch_log
        WHERE etf_code = ? AND snapshot_date = ?
        ORDER BY fetch_id DESC
        LIMIT 1
        """,
        (etf_code, snapshot_date),
    ).fetchone()


def list_etf_holdings_fetch_log(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    snapshot_date: str | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM etf_holdings_fetch_log WHERE etf_code = ?"
    params: list[object] = [etf_code]
    if snapshot_date:
        sql += " AND snapshot_date = ?"
        params.append(snapshot_date)
    sql += " ORDER BY fetch_id DESC"
    return conn.execute(sql, params).fetchall()

