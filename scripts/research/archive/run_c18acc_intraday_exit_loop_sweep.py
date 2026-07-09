#!/usr/bin/env python3
"""C18acc POOL1 · intraday exit loop · XEL-0…XEL-3."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.c18acc_intraday_exit_loop import (  # noqa: E402
    merge_intraday_exit_loop_payloads,
    render_intraday_exit_loop_md,
    run_intraday_exit_loop_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="POOL1 intraday exit loop sweep · Phase 3")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2024-01-01")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--is-end", default="2025-12-31")
    ap.add_argument(
        "--oos-h2-mode",
        choices=("forward", "historical"),
        default="forward",
    )
    ap.add_argument(
        "--variants",
        default=None,
        help="Comma-separated variant ids (e.g. XEL-2,XEL-3)",
    )
    ap.add_argument(
        "--window-labels",
        default=None,
        help="Comma-separated window labels (IS,FULL,OOS_H1,OOS_H2)",
    )
    ap.add_argument(
        "--merge-from",
        type=Path,
        action="append",
        default=None,
        help="Merge rows from prior JSON payloads after this run",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    variant_ids = None
    if args.variants:
        variant_ids = [v.strip() for v in args.variants.split(",") if v.strip()]
    window_labels = None
    if args.window_labels:
        window_labels = [w.strip() for w in args.window_labels.split(",") if w.strip()]

    conn = connect(args.db)
    try:
        payload = run_intraday_exit_loop_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            oos_h2_mode=args.oos_h2_mode,
            variant_ids=variant_ids,
            window_labels=window_labels,
        )
    finally:
        conn.close()

    if args.merge_from:
        extras = [json.loads(p.read_text(encoding="utf-8")) for p in args.merge_from]
        payload = merge_intraday_exit_loop_payloads(
            *extras,
            payload,
            date_start=args.date_start,
            date_end=args.date_end,
        )

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_intraday_exit_loop.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_intraday_exit_loop_md(payload), encoding="utf-8")

    print(payload.get("verdict", {}).get("summary", ""))
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
