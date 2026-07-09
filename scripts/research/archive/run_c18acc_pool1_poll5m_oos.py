#!/usr/bin/env python3
"""C18acc POOL1 vs S2 · poll_5m · OOS H1/H2 holdout."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.c18acc_drs_extension_overlay_sweep import (  # noqa: E402
    render_pool1_poll5m_oos_md,
    run_pool1_poll5m_oos_holdout,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2024-01-01")
    ap.add_argument("--date-end", default=None, help="default: DB latest")
    ap.add_argument("--is-end", default="2025-12-31")
    ap.add_argument(
        "--oos-h2-mode",
        choices=("forward", "historical"),
        default="forward",
        help="forward=2026 H2 live holdout · historical=2025-07-01..2025-12-31 proxy",
    )
    ap.add_argument(
        "--min-oos-h2-days",
        type=int,
        default=20,
        help="Min trading days in OOS H2 before evaluating (default 20)",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_pool1_poll5m_oos_holdout(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            oos_h2_mode=args.oos_h2_mode,
            min_oos_h2_trade_dates=args.min_oos_h2_days,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_pool1_poll5m_oos.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_pool1_poll5m_oos_md(payload), encoding="utf-8")

    print(payload.get("verdict", {}).get("summary", ""))
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
