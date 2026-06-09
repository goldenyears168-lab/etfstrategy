#!/usr/bin/env python3
"""終端報告摘要：執行評估 / 收盤資料健康 / 週日補庫（只讀 stocks.db）。"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from stock_context import compute_technical_tej
from sync_morning_futures import format_morning_risk_line, morning_radar_warnings
from execution_timeline import layer_heading, print_execution_timeline
from pre_trade_check import load_tsm_adr_pct
from stock_db import (
    DEFAULT_DB_PATH,
    PROJECT_ROOT,
    connect,
    load_execution_tx_gap,
    load_latest_morning_risk,
    load_latest_tech_risk,
)
from operational_brief import print_morning_checklist
from position_review import print_morning_position_exits
from order_intent_engine import print_morning_execution_summary
from pm_watchlist import print_morning_pm_conclusion
from portfolio_engine import print_morning_portfolio_summary

TW_SPOT_CODE = "IX0001"
ETF_CODES = (
    "00981A",
    "00403A",
    "009816",
    "00980A",
    "00982A",
    "00992A",
)
HOLDINGS_ETFS = ETF_CODES + ("00407A",)


def _fmt_pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _overnight_regime_line(conn: sqlite3.Connection, trade_date: str | None = None) -> str | None:
    row = load_latest_tech_risk(conn, trade_date=trade_date)
    if row is None:
        return None
    sox = row["sox_daily_return_pct"]
    tsm = load_tsm_adr_pct(conn, trade_date=trade_date)
    us = row["us_trade_date"] if "us_trade_date" in row.keys() else None
    us_part = f" 美股日 {us}" if us else ""
    return (
        f"台股日 {row['session_date']}{us_part}  TSM {_fmt_pct(tsm)}  "
        f"半導體 {_fmt_pct(sox)}  台指gap(隔夜) {_fmt_pct(row['tx_gap_pct'])}  "
        f"電子期(隔夜) {_fmt_pct(row['te_overnight_pct'])}"
    )


def _tech_line(conn: sqlite3.Connection, trade_date: str | None = None) -> str | None:
    return _overnight_regime_line(conn, trade_date=trade_date)


def _safe_query(conn: sqlite3.Connection, sql: str, params=()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _load_tsm_adr_ma_line(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute(
            """
            SELECT session_date, tsm_vs_ma5_pct, tsm_vs_ma10_pct,
                   tsm_above_ma5, tsm_above_ma10
            FROM tech_risk_daily_snapshot
            ORDER BY session_date DESC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return (
        f"TSM ADR 乖離 MA5 {_fmt_pct(row['tsm_vs_ma5_pct'])} "
        f"MA10 {_fmt_pct(row['tsm_vs_ma10_pct'])} "
        f"(站上MA5={'Y' if row['tsm_above_ma5'] else 'N'}/MA10={'Y' if row['tsm_above_ma10'] else 'N'})"
    )


def print_execution_eval_report(
    conn: sqlite3.Connection,
    *,
    trade_date: str | None = None,
    evaluation_mode: str = "pre_open",
    price_source: str = "last_close",
    eval_run_id: str | None = None,
    persist_intents: bool | None = None,
    price_snapshots: dict[str, float] | None = None,
) -> None:
    from datetime import datetime
    from order_intent_engine import parse_trade_date
    from zoneinfo import ZoneInfo

    td = parse_trade_date(trade_date)
    evaluated_at = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")
    print("")
    print(f"=== {layer_heading(evaluation_mode)} ===")
    print(
        f"  快照 {evaluated_at} · trade_date={td} · "
        f"price_source={price_source}"
        + (f" · run={eval_run_id}" if eval_run_id else "")
    )
    print_execution_timeline(
        evaluation_mode,
        trade_date=td,
        price_source=price_source,
    )
    _print_execution_eval_body(
        conn,
        trade_date=td,
        evaluation_mode=evaluation_mode,
        persist_intents=persist_intents,
        price_snapshots=price_snapshots,
    )


def _print_execution_eval_body(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    evaluation_mode: str = "pre_open",
    persist_intents: bool | None = None,
    price_snapshots: dict[str, float] | None = None,
) -> None:
    if evaluation_mode != "pre_open":
        print("")
        print("  （本層不重跑 TEJ ingest；僅重算執行快照與建議掛單價）")

    if evaluation_mode == "pre_open":
        print("")
        print("=== 隔夜體制（研究用 · tech_risk_daily_snapshot）===")
    line = _overnight_regime_line(conn, trade_date=trade_date)
    if line:
        print(f"  {line}")
    else:
        print("  tech_risk_daily_snapshot  —（尚無資料，請先跑 tech risk 同步）")
    ma_line = _load_tsm_adr_ma_line(conn)
    if ma_line:
        print(f"  {ma_line}")

    if evaluation_mode == "pre_open":
        print("")
        print("=== 開盤執行雷達（morning_risk_snapshot · 即時）===")
        morning = load_latest_morning_risk(conn, trade_date=trade_date)
        if morning is not None:
            print(f"  {format_morning_risk_line(morning)}")
            for warn in morning_radar_warnings(morning):
                print(f"  {warn}")
            if morning["notes"]:
                print(f"  備註：{morning['notes']}")
        else:
            print("  morning_risk_snapshot  —（尚無資料；請先跑 sync_morning_futures）")
        gap_val, gap_src = load_execution_tx_gap(conn, trade_date=trade_date)
        if gap_val is not None:
            print(f"  執行限價用 gap：{_fmt_pct(gap_val)}（{gap_src}）")

    if evaluation_mode == "pre_open":
        print("")
        print("=== 環境快照（pre_open 同步後）===")

    ix = compute_technical_tej(conn, TW_SPOT_CODE)
    if ix is not None and ix.dist_ma20_pct is not None:
        print(
            f"  {TW_SPOT_CODE} TEJ 技術  收盤 {ix.close}  "
            f"MA20 {ix.dist_ma20_pct:+.1f}%  MA60 {ix.dist_ma60_pct:+.1f}%  "
            f"52週位 {ix.position_52w_pct:.0f}%  距52週高 {ix.dist_from_52w_high_pct:+.1f}%"
        )
    elif ix is not None:
        print(f"  {TW_SPOT_CODE} TEJ 技術  收盤 {ix.close}（歷史不足 MA/52週）")

    bars = _safe_query(
        conn,
        """
        SELECT code, MAX(date) AS latest
        FROM daily_bars
        WHERE code IN ('00981A','00403A','009816','00980A','00982A','00992A','IX0001')
          AND source = 'tej'
        GROUP BY code
        ORDER BY code
        """,
    )
    if bars:
        print("  daily_bars（TEJ 最新交易日）")
        for r in bars:
            print(f"    {r['code']}  {r['latest']}")
    else:
        print("  daily_bars  —")

    sig = _safe_query(
        conn,
        """
        SELECT code, MAX(snapshot_date) AS latest
        FROM etf_daily_signal_snapshot
        WHERE code IN ('00981A','00403A','009816','00980A','00982A','00992A')
        GROUP BY code
        ORDER BY code
        """,
    )
    if os.environ.get("ENABLE_FINMIND_SIGNAL", "0") == "1":
        if sig:
            print("  etf_daily_signal_snapshot（FinMind）")
            for r in sig:
                print(f"    {r['code']}  {r['latest']}")
        else:
            print("  etf_daily_signal_snapshot  —（已啟用 ENABLE_FINMIND_SIGNAL 但尚無列）")
    else:
        print("  etf_daily_signal_snapshot  SKIP（ENABLE_FINMIND_SIGNAL=0）")
    if evaluation_mode == "pre_open":
        print("")
        print("=== 研究參考（② 收盤凍結 · 不隨本層重算）===")
        print_morning_pm_conclusion(conn)
        print_morning_portfolio_summary(conn)
    if persist_intents is None:
        persist_intents = os.environ.get("RUN_ORDER_INTENT", "1") == "1"
    print_morning_execution_summary(
        conn,
        trade_date=trade_date,
        persist=persist_intents,
        evaluation_mode=evaluation_mode,
        price_snapshots=price_snapshots,
    )
    if evaluation_mode == "pre_open":
        print("")
        print("=== 核對用 Checklist ===")
        print_morning_checklist(conn, ETF_CODES)
        print_morning_position_exits(conn)
    print("  完整同步細節 → logs/daily_sync_YYYYMMDD.log")


def print_morning_report(conn: sqlite3.Connection) -> None:
    """Deprecated alias：等同 pre_open 執行評估。"""
    print_execution_eval_report(conn)


def print_evening_data_health(conn: sqlite3.Connection) -> None:
    print("")
    print("=== 資料健康（寫入 stocks.db）===")
    line = _tech_line(conn)
    if line:
        print(f"  tech_risk_daily_snapshot  {line}")

    meta = _safe_query(
        conn,
        """
        SELECT etf_code, MAX(snapshot_date) AS latest, MAX(holding_count) AS n
        FROM etf_holdings_meta
        WHERE etf_code IN (
            '00981A','00403A','009816','00407A','00980A','00982A','00992A'
        )
        GROUP BY etf_code
        ORDER BY etf_code
        """,
    )
    if meta:
        parts = [f"{r['etf_code']} {r['latest']}" for r in meta]
        print(f"  etf_holdings_meta  {' · '.join(parts)}")

    cat = _safe_query(
        conn,
        """
        SELECT source, COUNT(*) AS n
        FROM catalyst_events
        GROUP BY source
        ORDER BY source
        """,
    )
    if cat:
        print(
            "  catalyst_events  "
            + " · ".join(f"{r['source']} {r['n']} 筆" for r in cat)
        )
    else:
        news = os.environ.get("RUN_NEWS_SYNC", "0")
        if news == "1" and os.environ.get("PERPLEXITY_API_KEY", "").strip():
            print("  catalyst_events  0 筆（Perplexity 可能無新事件或 API 失敗）")
        else:
            print(
                "  catalyst_events  —（預設人工上網查；可選 RUN_NEWS_SYNC=1 + Perplexity）"
            )

    score = _safe_query(
        conn,
        "SELECT MAX(as_of_date) AS d, COUNT(*) AS n FROM investment_scores",
    )
    if score and score[0]["d"]:
        print(
            f"  investment_scores  最新 {score[0]['d']} · {score[0]['n']} 列"
            f"（RUN_SCORE_ENGINE={os.environ.get('RUN_SCORE_ENGINE', '0')}）"
        )
    else:
        print(
            f"  investment_scores  未更新（RUN_SCORE_ENGINE="
            f"{os.environ.get('RUN_SCORE_ENGINE', '0')}）"
        )

    pm = _safe_query(
        conn,
        """
        SELECT MAX(as_of_date) AS d, COUNT(*) AS n
        FROM pm_watchlist WHERE score_version = 'p4-v2'
        """,
    )
    if pm and pm[0]["d"]:
        br = _safe_query(
            conn,
            """
            SELECT pm_bucket, COUNT(*) AS n
            FROM pm_watchlist
            WHERE as_of_date = ? AND score_version = 'p4-v2'
            GROUP BY pm_bucket
            """,
            (pm[0]["d"],),
        )
        parts = [f"{r['pm_bucket']} {r['n']}" for r in br] if br else []
        print(
            f"  pm_watchlist  最新 {pm[0]['d']} · {pm[0]['n']} 列"
            + (f"（{' · '.join(parts)}）" if parts else "")
        )
    else:
        print("  pm_watchlist  —（收盤 Score --sync-db 後寫入）")

    pw = _safe_query(
        conn,
        """
        SELECT MAX(as_of_date) AS d, COUNT(*) AS n,
               SUM(CASE WHEN portfolio_weight_pct > 0 THEN 1 ELSE 0 END) AS alloc
        FROM portfolio_weights WHERE score_version = 'p4-v2'
        """,
    )
    if pw and pw[0]["d"]:
        cap = _safe_query(
            conn,
            """
            SELECT capital_ntd FROM portfolio_weights
            WHERE as_of_date = ? AND score_version = 'p4-v2' LIMIT 1
            """,
            (pw[0]["d"],),
        )
        capital = float(cap[0]["capital_ntd"]) if cap else 0
        print(
            f"  portfolio_weights  最新 {pw[0]['d']} · 配置 {pw[0]['alloc']}/{pw[0]['n']} 檔"
            f"（資金 {capital:,.0f} NTD）"
        )
    else:
        print("  portfolio_weights  —")

    memo = _safe_query(conn, "SELECT MAX(memo_date) AS d FROM research_memos")
    if memo and memo[0]["d"]:
        print(f"  research_memos  最新 {memo[0]['d']}")
    from datetime import date as _date

    brief = PROJECT_ROOT / "reports" / f"{_date.today().strftime('%Y%m%d')}_evening_summary.md"
    if brief.exists():
        print(f"  reports  收盤摘要 → {brief.name}")
    print("  詳細表與同步步驟 → logs/daily_sync_YYYYMMDD.log")


def print_weekly_report(conn: sqlite3.Connection) -> None:
    print("")
    print("=== 週日深度補庫摘要（stocks.db）===")
    beta = _safe_query(
        conn,
        """
        SELECT COUNT(*) AS n, MAX(as_of_date) AS latest,
               AVG(beta) AS avg_beta
        FROM stock_beta WHERE source = 'yahoo_computed'
        """,
    )
    if beta and beta[0]["n"]:
        print(
            f"  stock_beta  {beta[0]['n']} 檔 · 最新 as_of {beta[0]['latest']} · "
            f"平均 β {float(beta[0]['avg_beta'] or 0):.2f}"
        )
    else:
        print("  stock_beta  —")

    for table, label in (
        ("stock_fundamental", "stock_fundamental（L8）"),
        ("stock_consensus", "stock_consensus（L8.5）"),
        ("stock_financial_history", "stock_financial_history"),
    ):
        row = _safe_query(
            conn,
            f"SELECT COUNT(DISTINCT stock_id) AS stocks, MAX(as_of_date) AS latest "
            f"FROM {table}",
        )
        if row and row[0]["stocks"]:
            print(f"  {label}  {row[0]['stocks']} 檔 · 最新 {row[0]['latest']}")
        else:
            print(f"  {label}  —")

    mkt = _safe_query(
        conn,
        """
        SELECT COUNT(DISTINCT stock_id) AS stocks, MAX(trade_date) AS latest
        FROM stock_daily_bars WHERE source = 'finmind'
        """,
    )
    if mkt and mkt[0]["stocks"]:
        print(
            f"  stock_daily_bars  {mkt[0]['stocks']} 檔 · 最新 {mkt[0]['latest']}"
        )
    elif os.environ.get("RUN_STOCK_MARKET_SYNC", "0") == "1":
        print("  stock_daily_bars  —（已啟用 RUN_STOCK_MARKET_SYNC 但尚無列）")
    else:
        print("  stock_daily_bars  SKIP（RUN_STOCK_MARKET_SYNC=0）")
    from trade_levels import levels_path_hint

    print(f"  執行上下文  收盤 Universe 後印籌碼/量/技術；R:R 見 {levels_path_hint()}")
    print("  完整步驟 → logs/weekly_sync_YYYYMMDD.log")


def main() -> int:
    parser = argparse.ArgumentParser(description="Report summary from stocks.db")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--mode",
        required=True,
        choices=("execution-eval", "morning", "evening-health", "evening-digest", "weekly"),
    )
    args = parser.parse_args()
    if not args.db.exists():
        print(f"ERROR: DB 不存在 {args.db}", file=sys.stderr)
        return 1
    conn = connect(args.db)
    try:
        if args.mode in ("execution-eval", "morning"):
            if args.mode == "morning":
                print(
                    "  （提示：--mode morning 已更名為 execution-eval）",
                    file=sys.stderr,
                )
            print_execution_eval_report(conn)
        elif args.mode == "evening-health":
            print_evening_data_health(conn)
        elif args.mode == "evening-digest":
            from evening_digest import print_evening_human_digest
            from research_universe import parse_etf_codes

            print_evening_human_digest(conn, parse_etf_codes(",".join(ETF_CODES)))
        else:
            print_weekly_report(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
