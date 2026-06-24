#!/usr/bin/env python3
"""補足 rrg-lens-score-swap 回測用 SQLite：RRG close 歷史 · lens_daily_highlight · stock_kbar_1m。"""

from __future__ import annotations

import argparse
import sys
import time
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from backfill_yahoo_research import sync_rrg_history  # noqa: E402
from finmind_client import fetch_finmind, finmind_token  # noqa: E402
from project_config import DEFAULT_ETF_CODES  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_daily_lens import build_stock_daily_lens_rows  # noqa: E402
from stock_db import (  # noqa: E402
    DEFAULT_DB_PATH,
    connect,
    count_lens_daily_highlight_dates,
    finmind_kbar_rows_to_db,
    kbar_day_has_data,
    load_etf_constituent_watchlist,
    load_lens_daily_highlight,
    upsert_lens_daily_highlight,
    upsert_stock_kbar_1m,
)
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar  # noqa: E402
from research.backtest.rrg_mono_intraday_ab import close_shortlist  # noqa: E402
from yahoo_chart_sync import YAHOO_KBAR_SOURCE, fetch_tw_intraday_kbar_rows  # noqa: E402

DEFAULT_START = "2024-01-01"
DEFAULT_END = "2026-06-22"
ALL_LAYERS = ("rrg", "lens", "kbar")


def _trade_dates_in_range(conn, *, start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date AS d FROM stock_daily_bars
        WHERE source = 'finmind' AND trade_date BETWEEN ? AND ?
        ORDER BY d
        """,
        (start, end),
    ).fetchall()
    return [str(r["d"]) for r in rows]


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def _month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        last_day = monthrange(cursor.year, cursor.month)[1]
        month_end = date(cursor.year, cursor.month, last_day)
        chunk_start = max(cursor, start)
        chunk_end = min(month_end, end)
        if chunk_start <= chunk_end:
            chunks.append((chunk_start, chunk_end))
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return chunks


def report_gaps(
    conn,
    *,
    start: str,
    end: str,
) -> dict[str, object]:
    trade_dates = _trade_dates_in_range(conn, start=start, end=end)
    rrg_days = {
        str(r[0])
        for r in conn.execute(
            """
            SELECT DISTINCT session_date FROM rrg_universe_scores
            WHERE screen_kind = 'close' AND session_date BETWEEN ? AND ?
            """,
            (start, end),
        ).fetchall()
    }
    lens_days = count_lens_daily_highlight_dates(conn, start=start, end=end)
    kbar_row = conn.execute(
        """
        SELECT COUNT(*) AS rows, COUNT(DISTINCT stock_id) AS stocks,
               COUNT(DISTINCT trade_date) AS days
        FROM stock_kbar_1m
        WHERE trade_date BETWEEN ? AND ?
        """,
        (start, end),
    ).fetchone()
    pair_row = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT stock_id, trade_date FROM stock_kbar_1m
            WHERE trade_date BETWEEN ? AND ?
            GROUP BY stock_id, trade_date
        )
        """,
        (start, end),
    ).fetchone()
    watch_n = len(load_etf_constituent_watchlist(conn, DEFAULT_ETF_CODES))
    expected_kbar_pairs = len(trade_dates) * watch_n
    actual_pairs = int(pair_row[0] or 0) if pair_row else 0
    return {
        "trade_days": len(trade_dates),
        "rrg_close_days": len(rrg_days),
        "rrg_missing_days": len([d for d in trade_dates if d not in rrg_days]),
        "lens_highlight_days": lens_days,
        "lens_missing_days": max(0, len(trade_dates) - lens_days),
        "kbar_rows": int(kbar_row[0] or 0) if kbar_row else 0,
        "kbar_stock_days": actual_pairs,
        "kbar_expected_stock_days": expected_kbar_pairs,
        "watchlist_stocks": watch_n,
    }


def backfill_rrg_close(
    conn,
    *,
    start: date,
    end: date,
    quiet: bool,
) -> int:
    return sync_rrg_history(conn, start, end, quiet=quiet)


def backfill_lens_highlight(
    conn,
    *,
    start: str,
    end: str,
    force: bool,
    quiet: bool,
    light_regime: bool,
) -> int:
    dates = _trade_dates_in_range(conn, start=start, end=end)
    if not dates:
        return 0
    written = 0
    prev_rows: list[dict] | None = None
    prev_date = conn.execute(
        """
        SELECT MAX(trade_date) FROM lens_daily_highlight
        WHERE trade_date < ?
        """,
        (dates[0],),
    ).fetchone()
    if prev_date and prev_date[0]:
        prev_rows = load_lens_daily_highlight(conn, str(prev_date[0]))

    for trade_date in dates:
        existing = conn.execute(
            "SELECT COUNT(*) FROM lens_daily_highlight WHERE trade_date = ?",
            (trade_date,),
        ).fetchone()
        if not force and existing and int(existing[0] or 0) > 0:
            prev_rows = load_lens_daily_highlight(conn, trade_date)
            continue
        rows = build_stock_daily_lens_rows(
            conn,
            trade_date,
            prev_highlight_rows=prev_rows,
            light_regime=light_regime,
        )
        row_dicts = [r.to_db_dict() for r in rows]
        n = upsert_lens_daily_highlight(conn, row_dicts)
        written += n
        prev_rows = row_dicts
        if not quiet:
            print(f"  lens {trade_date}: rows={n}")
    return written


def _kbar_day_complete(conn, stock_id: str, trade_date: str) -> bool:
    return kbar_day_has_data(conn, stock_id, trade_date)


def _yahoo_stock_complete(
    conn,
    stock_id: str,
    trade_dates: list[str],
    *,
    source: str = YAHOO_KBAR_SOURCE,
) -> bool:
    if not trade_dates:
        return True
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT trade_date) AS n FROM stock_kbar_1m
        WHERE stock_id = ? AND trade_date BETWEEN ? AND ? AND source = ?
        """,
        (stock_id, trade_dates[0], trade_dates[-1], source),
    ).fetchone()
    return int(row[0] or 0) >= len(trade_dates) if row else False


def _date_chunks(start: date, end: date, *, max_days: int) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=max_days - 1))
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def backfill_stock_kbar_yahoo(
    conn,
    *,
    start: date,
    end: date,
    stock_ids: list[str],
    force: bool,
    delay: float,
    quiet: bool,
    kbar_1m_only: bool = False,
    recent_days: int = 30,
) -> int:
    """Yahoo Chart · 預設 1h 全區間 + 近端 1m；kbar_1m_only 僅拉最近 N 日 1m。"""
    today = date.today()
    if kbar_1m_only:
        start = max(start, today - timedelta(days=recent_days - 1))
        end = min(end, today)
    trade_dates = _trade_dates_in_range(conn, start=start.isoformat(), end=end.isoformat())
    if not trade_dates:
        return 0
    recent_start = max(start, today - timedelta(days=recent_days - 1))
    total = 0
    for i, sid in enumerate(stock_ids, start=1):
        if not force and _yahoo_stock_complete(conn, sid, trade_dates):
            continue
        sym: str | None = None
        if not kbar_1m_only:
            rows_h, sym = fetch_tw_intraday_kbar_rows(sid, start, end, interval="1h")
            if rows_h:
                total += upsert_stock_kbar_1m(conn, rows_h)
                if not quiet:
                    days_h = len({r["trade_date"] for r in rows_h})
                    print(f"  yahoo 1h {sid} ({sym}): bars={len(rows_h)} days={days_h}")
            time.sleep(delay)
        m_start = recent_start if not kbar_1m_only else start
        if end >= m_start:
            for c0, c1 in _date_chunks(m_start, end, max_days=7):
                rows_m, sym_m = fetch_tw_intraday_kbar_rows(sid, c0, c1, interval="1m")
                if rows_m:
                    total += upsert_stock_kbar_1m(conn, rows_m)
                    sym = sym or sym_m
                time.sleep(delay)
            if not quiet and sym:
                n_m = conn.execute(
                    """
                    SELECT COUNT(*) FROM stock_kbar_1m
                    WHERE stock_id = ? AND trade_date >= ? AND source = ?
                    """,
                    (sid, m_start.isoformat(), YAHOO_KBAR_SOURCE),
                ).fetchone()
                print(f"  yahoo 1m {sid} ({sym}): recent_bars={int(n_m[0] or 0)}")
        if not quiet and (i % 10 == 0 or i == len(stock_ids)):
            print(f"  yahoo kbar progress {i}/{len(stock_ids)} bars_upserted={total}")
    return total


def collect_mono_shortlist_kbar_gaps(
    conn,
    *,
    start: str,
    end: str,
) -> list[tuple[str, str]]:
    """RRG mono fresh shortlist 上缺 1m K 的 (trade_date, stock_id) 對。"""
    trade_dates = _trade_dates_in_range(conn, start=start, end=end)
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    gaps: list[tuple[str, str]] = []
    for trade_date in trade_dates:
        for row in close_shortlist(fresh_by_date.get(trade_date, [])):
            if not kbar_day_has_data(conn, row.stock_id, trade_date):
                gaps.append((trade_date, row.stock_id))
    return gaps


def backfill_stock_kbar_finmind(
    conn,
    *,
    start: date,
    end: date,
    stock_ids: list[str],
    force: bool,
    delay: float,
    quiet: bool,
    pairs: list[tuple[str, str]] | None = None,
) -> int:
    """FinMind TaiwanStockKBar · 單日一請求（API 不支援多日區間）。

    pairs 非空時僅補指定 (trade_date, stock_id)，略過全 watchlist 掃描。
    """
    if not finmind_token():
        raise RuntimeError("FINMIND_TOKEN 未設定，無法拉 TaiwanStockKBar")
    total = 0
    if pairs is not None:
        work = [
            (d, sid)
            for d, sid in pairs
            if start.isoformat() <= d <= end.isoformat() and (force or not _kbar_day_complete(conn, sid, d))
        ]
        for i, (trade_date, sid) in enumerate(work, start=1):
            d = _parse_date(trade_date)
            try:
                raw = fetch_finmind("TaiwanStockKBar", sid, d, d)
            except Exception as exc:
                if not quiet:
                    print(f"  kbar {sid} {trade_date} warn: {exc}")
                time.sleep(delay)
                continue
            rows = finmind_kbar_rows_to_db(sid, raw)
            if rows:
                total += upsert_stock_kbar_1m(conn, rows)
                if not quiet and (i <= 3 or i % 50 == 0 or i == len(work)):
                    print(f"  kbar {sid} {trade_date}: bars={len(rows)} [{i}/{len(work)}]")
            time.sleep(delay)
        return total

    trade_dates = _trade_dates_in_range(conn, start=start.isoformat(), end=end.isoformat())
    if not trade_dates:
        return 0
    for i, sid in enumerate(stock_ids, start=1):
        for trade_date in trade_dates:
            if not force and _kbar_day_complete(conn, sid, trade_date):
                continue
            d = _parse_date(trade_date)
            try:
                raw = fetch_finmind("TaiwanStockKBar", sid, d, d)
            except Exception as exc:
                if not quiet:
                    print(f"  kbar {sid} {trade_date} warn: {exc}")
                time.sleep(delay)
                continue
            rows = finmind_kbar_rows_to_db(sid, raw)
            if rows:
                total += upsert_stock_kbar_1m(conn, rows)
                if not quiet and len(trade_dates) <= 5:
                    print(f"  kbar {sid} {trade_date}: bars={len(rows)}")
            time.sleep(delay)
        if not quiet and (i % 10 == 0 or i == len(stock_ids)):
            print(f"  kbar progress {i}/{len(stock_ids)} bars_upserted={total}")
    return total


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="Backfill SQLite for rrg-lens-score-swap research")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument(
        "--layers",
        default=",".join(ALL_LAYERS),
        help=f"Comma-separated: {','.join(ALL_LAYERS)}",
    )
    parser.add_argument("--report", action="store_true", help="只印缺口報告")
    parser.add_argument("--force", action="store_true", help="覆寫已有列")
    parser.add_argument("--delay", type=float, default=0.25, help="API 請求間隔秒")
    parser.add_argument(
        "--kbar-provider",
        choices=("yahoo", "finmind"),
        default="yahoo",
        help="K 線來源：yahoo（預設）或 finmind",
    )
    parser.add_argument(
        "--kbar-1m-only",
        action="store_true",
        help="Yahoo 僅補最近 N 日 1m（跳過 1h 全區間）",
    )
    parser.add_argument(
        "--kbar-recent-days",
        type=int,
        default=30,
        help="--kbar-1m-only 時的回看日數（預設 30）",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--lens-light",
        action="store_true",
        help="監控清單 backfill 略過 Regime 重算（池成員與 RRG/VCP 不變，較快）",
    )
    parser.add_argument(
        "--shortlist-only",
        action="store_true",
        help="kbar 層僅補 RRG mono fresh shortlist 缺口（建議搭配 --kbar-provider finmind）",
    )
    args = parser.parse_args(argv)

    layers = tuple(x.strip() for x in args.layers.split(",") if x.strip())
    bad = [x for x in layers if x not in ALL_LAYERS]
    if bad:
        parser.error(f"unknown layers: {bad}")

    conn = connect(args.db)
    try:
        gaps = report_gaps(conn, start=args.start, end=args.end)
        print("=== backtest data gaps ===")
        for key, val in gaps.items():
            print(f"  {key}: {val}")

        if args.report:
            return 0

        start_d = _parse_date(args.start)
        end_d = _parse_date(args.end)

        if "rrg" in layers:
            print("--- rrg_universe_scores close ---")
            n = backfill_rrg_close(conn, start=start_d, end=end_d, quiet=args.quiet)
            print(f"  rows_written≈{n}")

        if "lens" in layers:
            print("--- lens_daily_highlight ---")
            n = backfill_lens_highlight(
                conn,
                start=args.start,
                end=args.end,
                force=args.force,
                quiet=args.quiet,
                light_regime=args.lens_light,
            )
            print(f"  rows_written={n}")

        if "kbar" in layers:
            print(f"--- stock_kbar_1m ({args.kbar_provider}) ---")
            stock_ids = [
                w["stock_id"] for w in load_etf_constituent_watchlist(conn, DEFAULT_ETF_CODES)
            ]
            gap_pairs: list[tuple[str, str]] | None = None
            if args.shortlist_only:
                gap_pairs = collect_mono_shortlist_kbar_gaps(
                    conn, start=args.start, end=args.end
                )
                print(f"  shortlist gaps: {len(gap_pairs)} stock-days")
            if args.kbar_provider == "yahoo":
                if args.shortlist_only:
                    parser.error("--shortlist-only 目前僅支援 --kbar-provider finmind")
                n = backfill_stock_kbar_yahoo(
                    conn,
                    start=start_d,
                    end=end_d,
                    stock_ids=stock_ids,
                    force=args.force,
                    delay=max(0.35, args.delay),
                    quiet=args.quiet,
                    kbar_1m_only=args.kbar_1m_only,
                    recent_days=args.kbar_recent_days,
                )
            else:
                n = backfill_stock_kbar_finmind(
                    conn,
                    start=start_d,
                    end=end_d,
                    stock_ids=stock_ids,
                    force=args.force,
                    delay=args.delay,
                    quiet=args.quiet,
                    pairs=gap_pairs,
                )
            print(f"  bars_upserted={n}")

        gaps2 = report_gaps(conn, start=args.start, end=args.end)
        print("=== after backfill ===")
        for key, val in gaps2.items():
            print(f"  {key}: {val}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
