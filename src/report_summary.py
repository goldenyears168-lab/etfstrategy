#!/usr/bin/env python3
"""終端報告摘要：收盤資料健康 / 週日補庫（只讀 stocks.db）。"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from operational_brief import load_tsm_adr_pct
from project_config import active_score_version
from stock_db import DEFAULT_DB_PATH, PROJECT_ROOT, connect, load_latest_tech_risk

ETF_CODES = (
    "00981A",
    "00403A",
    "009816",
    "00980A",
    "00982A",
    "00992A",
)


def _fmt_pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _safe_query(conn: sqlite3.Connection, sql: str, params=()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def print_evening_data_health(conn: sqlite3.Connection) -> None:
    print("")
    print("=== 資料健康（stocks.db）===")
    row = load_latest_tech_risk(conn)
    if row:
        tsm = load_tsm_adr_pct(conn)
        print(
            f"  tech_risk  台股日 {row['session_date']} · TSM {_fmt_pct(tsm)} · "
            f"台指gap {_fmt_pct(row['tx_gap_pct'])}"
        )

    meta = _safe_query(
        conn,
        """
        SELECT etf_code, MAX(snapshot_date) AS latest
        FROM etf_holdings_meta
        WHERE etf_code IN (
            '00981A','00403A','009816','00407A','00980A','00982A','00992A'
        )
        GROUP BY etf_code ORDER BY etf_code
        """,
    )
    if meta:
        parts = [f"{r['etf_code']} {r['latest']}" for r in meta]
        print(f"  etf_holdings_meta  {' · '.join(parts)}")

    score = _safe_query(
        conn, "SELECT MAX(as_of_date) AS d, COUNT(*) AS n FROM investment_scores"
    )
    if score and score[0]["d"]:
        print(f"  investment_scores  最新 {score[0]['d']} · {score[0]['n']} 列")
    else:
        print("  investment_scores  —")

    pm = _safe_query(
        conn,
        """
        SELECT MAX(as_of_date) AS d, COUNT(*) AS n
        FROM pm_watchlist WHERE score_version = ?
        """,
        (active_score_version(),),
    )
    if pm and pm[0]["d"]:
        print(f"  pm_watchlist  最新 {pm[0]['d']} · {pm[0]['n']} 列")
    print("  詳細 → logs/daily_sync_YYYYMMDD.log")


def print_weekly_report(conn: sqlite3.Connection) -> None:
    print("")
    print("=== 週日深度補庫摘要 ===")
    beta = _safe_query(
        conn,
        """
        SELECT COUNT(*) AS n, MAX(as_of_date) AS latest
        FROM stock_beta WHERE source = 'yahoo_computed'
        """,
    )
    if beta and beta[0]["n"]:
        print(f"  stock_beta  {beta[0]['n']} 檔 · 最新 {beta[0]['latest']}")
    mkt = _safe_query(
        conn,
        """
        SELECT COUNT(DISTINCT stock_id) AS stocks, MAX(trade_date) AS latest
        FROM stock_daily_bars WHERE source = 'finmind'
        """,
    )
    if mkt and mkt[0]["stocks"]:
        print(f"  stock_daily_bars  {mkt[0]['stocks']} 檔 · 最新 {mkt[0]['latest']}")
    print("  完整 → logs/weekly_sync_YYYYMMDD.log")


def main() -> int:
    parser = argparse.ArgumentParser(description="Report summary from stocks.db")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--mode",
        required=True,
        choices=("evening-health", "weekly"),
    )
    args = parser.parse_args()
    if not args.db.exists():
        print(f"ERROR: DB 不存在 {args.db}", file=sys.stderr)
        return 1
    conn = connect(args.db)
    try:
        if args.mode == "evening-health":
            print_evening_data_health(conn)
        else:
            print_weekly_report(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
