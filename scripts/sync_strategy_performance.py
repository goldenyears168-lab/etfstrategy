#!/usr/bin/env python3
"""Recompute adopted-strategy yearly performance → SQLite + Supabase."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from project_dotenv import load_project_dotenv
from stock_db import DEFAULT_DB_PATH, connect
from strategy_performance_yearly import refresh_strategy_performance
from supabase_research_sync import dashboard_url, supabase_configured


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync strategy_performance_yearly")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--sqlite-only",
        action="store_true",
        help="Skip Supabase upsert",
    )
    parser.add_argument("--json", action="store_true", help="Print rows as JSON")
    args = parser.parse_args(argv)

    load_project_dotenv()
    if not args.sqlite_only and not supabase_configured():
        print(
            "Supabase 未設定：請在 .env 加入 SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY "
            "（或加 --sqlite-only）",
            file=sys.stderr,
        )
        return 2

    conn = connect(args.db)
    try:
        rows, uploaded = refresh_strategy_performance(
            conn,
            sync_supabase=not args.sqlite_only,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps([r.__dict__ for r in rows], ensure_ascii=False, indent=2))
    else:
        print(f"sqlite: {args.db} · rows={len(rows)}")
        for r in rows:
            sharpe = "—" if r.sharpe_ratio is None else f"{r.sharpe_ratio:.2f}"
            print(
                f"  · {r.strategy_id} {r.year_label}: "
                f"ret={r.total_return_pct:+.1f}% cagr={r.cagr_pct} "
                f"win={r.win_rate_vs_bench_pct}% sharpe={sharpe} n={r.n_periods}"
            )
        if uploaded:
            print(f"supabase: uploaded {len(uploaded)}")
            print(f"dashboard: {dashboard_url()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
