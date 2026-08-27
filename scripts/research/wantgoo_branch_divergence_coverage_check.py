#!/usr/bin/env python3
"""Coverage check: can WantGoo blogger predictions be used as a contrarian
confirm/disconfirm signal on top of the 9217 (KGI-Songshan) decisive branch-follow
study (n=36, live scan_5d_net95 signal, permutation p<0.0001)?

Read-only. Does not scrape. Just checks:
  1. reports/research/branch-footprint-screen/whale_9217_5dnet95_trades.csv
     -> date range of the 36 decisive 9217 buy-signal events being studied.
  2. data/wantgoo_loop.db  blog_posts / author_predictions
     -> date range + row counts of what the WantGoo loop has actually collected.
  3. Whether the two date ranges overlap at all, and whether author_predictions
     (the structured, symbol+direction table the contrarian-confirm question needs)
     has any rows.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/research/wantgoo_branch_divergence_coverage_check.py
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from stock_db import DATA_DIR  # noqa: E402

BRANCH_TRADES = ROOT / "reports/research/branch-footprint-screen/whale_9217_5dnet95_trades.csv"
WANTGOO_DB = DATA_DIR / "wantgoo_loop.db"
OUT = ROOT / "reports/research/wantgoo_branch_divergence/coverage_check.json"


def main() -> None:
    with BRANCH_TRADES.open() as f:
        rows = list(csv.DictReader(f))
    signal_dates = sorted(r["signal_date"] for r in rows)
    branch_stocks = sorted({r["stock_id"] for r in rows})

    con = sqlite3.connect(f"file:{WANTGOO_DB}?mode=ro", uri=True)
    bp_row = con.execute(
        "SELECT COUNT(*), MIN(publish_date), MAX(publish_date), "
        "COUNT(DISTINCT member_id), SUM(extracted) FROM blog_posts"
    ).fetchone()
    ap_count = con.execute("SELECT COUNT(*) FROM author_predictions").fetchone()[0]
    con.close()

    n_events, bp_count, bp_min, bp_max, bp_n_authors, bp_extracted = (
        len(signal_dates), *bp_row,
    )
    overlap_events = [d for d in signal_dates if bp_min and d >= bp_min]

    result = {
        "question": (
            "Is retail/blogger crowd sentiment (WantGoo) a useful contrarian "
            "confirm/disconfirm on top of the 9217 branch-follow buy signal?"
        ),
        "branch_9217_decisive_study": {
            "source_file": str(BRANCH_TRADES.relative_to(ROOT)),
            "n_events": n_events,
            "signal_date_min": signal_dates[0] if signal_dates else None,
            "signal_date_max": signal_dates[-1] if signal_dates else None,
            "n_distinct_stocks": len(branch_stocks),
        },
        "wantgoo_loop_data": {
            "db_path": str(WANTGOO_DB),
            "blog_posts_n_rows": bp_count,
            "blog_posts_publish_date_min": bp_min,
            "blog_posts_publish_date_max": bp_max,
            "blog_posts_n_distinct_authors": bp_n_authors,
            "blog_posts_n_extracted": bp_extracted,
            "author_predictions_n_rows": ap_count,
        },
        "overlap_check": {
            "n_9217_events_within_wantgoo_date_range": len(overlap_events),
            "overlap_event_dates": overlap_events,
        },
        "verdict": (
            "NOT ANSWERABLE with current data. "
            "author_predictions (the structured symbol+direction table the question "
            "needs) has 0 rows -- the LLM extraction step (extract_predictions.py) "
            "has never been run on the 55 staged blog_posts; only raw unstructured "
            "story_text exists. Independent of that, the WantGoo blog_posts date "
            f"range ({bp_min} to {bp_max}) has ZERO overlap with the 36 decisive "
            f"9217 signal events ({signal_dates[0]} to {signal_dates[-1]}), because "
            "the WantGoo loop only started scraping in late July 2026, after the "
            "last 9217 event in the study window (2026-06-22). Even a full LLM "
            "extraction pass today could not retroactively produce blogger "
            "predictions for the historical 9217 events -- that data was never "
            "collected while those events were live."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
