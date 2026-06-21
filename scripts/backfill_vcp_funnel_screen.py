#!/usr/bin/env python3
"""
歷史 backfill：對每個交易日重跑 vcp_funnel_screen → vcp_screen_scores_v2。

用法：
  PYTHONPATH=src python scripts/backfill_vcp_funnel_screen.py --report
  PYTHONPATH=src python scripts/backfill_vcp_funnel_screen.py --sync \\
    --date-start 2026-01-01 --date-end 2026-12-31

前置：stock_daily_bars（FinMind）與 daily_bars IX0001（TEJ）需覆蓋回測區間。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from vcp_funnel_screen import (  # noqa: E402
    LEGACY_MODEL_ID,
    MODEL_ID,
    load_vcp_funnel_params,
    run_vcp_funnel_screen,
)
from project_config import ETF_CODES_HOLDINGS, parse_etf_codes  # noqa: E402
from stock_db import (  # noqa: E402
    DEFAULT_DB_PATH,
    connect,
    delete_vcp_screen_scores_v2_for_model,
    load_vcp_screen_dates_for_model,
)


def load_trading_dates(
    conn,
    *,
    date_start: str,
    date_end: str,
    benchmark_code: str = "IX0001",
) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT date AS d
        FROM daily_bars
        WHERE code = ? AND source = 'tej'
          AND date >= ? AND date <= ?
        ORDER BY d ASC
        """,
        (benchmark_code, date_start, date_end),
    ).fetchall()
    if rows:
        return [str(r["d"]) for r in rows]
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date AS d
        FROM stock_daily_bars
        WHERE source = 'finmind'
          AND trade_date >= ? AND trade_date <= ?
        ORDER BY d ASC
        """,
        (date_start, date_end),
    ).fetchall()
    return [str(r["d"]) for r in rows]


def report_plan(
    conn,
    *,
    dates: list[str],
    skip_existing: bool,
) -> dict[str, int | list[str]]:
    existing = set(
        load_vcp_screen_dates_for_model(
            conn,
            model_id=MODEL_ID,
            date_start=dates[0] if dates else None,
            date_end=dates[-1] if dates else None,
        )
    )
    todo = [d for d in dates if not (skip_existing and d in existing)]
    return {
        "n_calendar": len(dates),
        "n_existing": len(existing & set(dates)),
        "n_todo": len(todo),
        "todo_dates": todo,
    }


def run_backfill(
    db_path: Path,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    skip_existing: bool,
    quiet: bool,
    dry_run: bool,
) -> dict[str, int]:
    conn = connect(db_path)
    stats = {
        "processed": 0,
        "written": 0,
        "skipped_existing": 0,
        "skipped_empty": 0,
        "errors": 0,
    }
    existing = set(
        load_vcp_screen_dates_for_model(
            conn,
            model_id=MODEL_ID,
            date_start=dates[0] if dates else None,
            date_end=dates[-1] if dates else None,
        )
    )
    params = load_vcp_funnel_params()
    t0 = time.monotonic()

    try:
        for i, as_of in enumerate(dates, 1):
            if skip_existing and as_of in existing:
                stats["skipped_existing"] += 1
                continue

            if dry_run:
                stats["processed"] += 1
                continue

            try:
                as_of_out, _results, layer_counts, _cfg = run_vcp_funnel_screen(
                    conn,
                    etf_codes=etf_codes,
                    params=params,
                    as_of_date=as_of,
                    persist=True,
                    replace_day=True,
                )
            except Exception as exc:
                stats["errors"] += 1
                print(f"ERROR {as_of}: {exc}", file=sys.stderr)
                continue

            stats["processed"] += 1
            n_l7 = layer_counts.get("L7", 0)
            if not as_of_out:
                stats["skipped_empty"] += 1
            else:
                stats["written"] += 1 if n_l7 else 0

            if not quiet and (i == 1 or i == len(dates) or i % 10 == 0):
                elapsed = time.monotonic() - t0
                rate = i / elapsed if elapsed > 0 else 0.0
                print(
                    f"[{i}/{len(dates)}] {as_of} "
                    f"L1={layer_counts.get('L1', 0)} L7={n_l7} "
                    f"({rate:.1f} d/s)",
                    flush=True,
                )
    finally:
        conn.close()

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VCP funnel 歷史篩選 backfill")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2026-01-01")
    parser.add_argument("--date-end", default="2026-12-31")
    parser.add_argument(
        "--etf-codes",
        default=",".join(ETF_CODES_HOLDINGS),
        help="ETF 成分 universe（預設 holdings 聯集）",
    )
    parser.add_argument("--report", action="store_true", help="僅列出待跑交易日")
    parser.add_argument("--sync", action="store_true", help="執行 backfill 寫 DB")
    parser.add_argument(
        "--force",
        action="store_true",
        help="重跑所有日期（覆寫當日 vcp-funnel 列；預設略過已有列的日期）",
    )
    parser.add_argument("--dry-run", action="store_true", help="計數但不寫 DB")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--purge-legacy",
        action="store_true",
        help=f"刪除 model_id={LEGACY_MODEL_ID} 全部列後再 backfill",
    )
    args = parser.parse_args(argv)

    skip_existing = not args.force
    etf_codes = parse_etf_codes(args.etf_codes)

    conn = connect(args.db)
    try:
        dates = load_trading_dates(
            conn, date_start=args.date_start, date_end=args.date_end
        )
    finally:
        conn.close()

    if not dates:
        print(f"找不到 {args.date_start}～{args.date_end} 交易日", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        plan = report_plan(conn, dates=dates, skip_existing=skip_existing)
    finally:
        conn.close()

    print(
        f"VCP funnel backfill · {args.date_start}～{args.date_end} · "
        f"calendar={plan['n_calendar']} existing={plan['n_existing']} todo={plan['n_todo']}"
    )
    if args.report and not args.sync:
        if plan["todo_dates"]:
            preview = plan["todo_dates"][:5]
            tail = plan["todo_dates"][-3:] if len(plan["todo_dates"]) > 8 else []
            print(f"  待跑範例：{preview}{['...'] if len(plan['todo_dates']) > 5 else []}{tail}")
        return 0

    if not args.sync and not args.dry_run:
        print("加上 --sync 或 --dry-run 以執行", file=sys.stderr)
        return 2

    if args.purge_legacy and args.sync and not args.dry_run:
        conn = connect(args.db)
        try:
            n_purged = delete_vcp_screen_scores_v2_for_model(conn, LEGACY_MODEL_ID)
        finally:
            conn.close()
        print(f"Purged {n_purged} rows (model_id={LEGACY_MODEL_ID})")

    stats = run_backfill(
        args.db,
        dates=dates,
        etf_codes=etf_codes,
        skip_existing=skip_existing,
        quiet=args.quiet,
        dry_run=args.dry_run,
    )
    print(
        f"Done: processed={stats['processed']} written_days={stats['written']} "
        f"skip_existing={stats['skipped_existing']} skip_empty={stats['skipped_empty']} "
        f"errors={stats['errors']}"
    )
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
