#!/usr/bin/env python3
"""Write copytrade L1H9 slot backtest JSON (from copytrade_runs DB)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_COPYTRADE_00981A  # noqa: E402
from research.backtest.slot_backtest_summary import (  # noqa: E402
    SlotBacktestConfig,
    build_summary_payload,
    compute_copytrade_slot_summary,
    write_slot_backtest_summary,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export copytrade L1H9 slot summary JSON")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--etf-code", default="00981A")
    parser.add_argument("--date-start", default="2026-01-01")
    parser.add_argument("--date-end", default="2026-12-31")
    parser.add_argument("--strategy-id", default="L1H9")
    parser.add_argument("--batch-id", default=None)
    parser.add_argument(
        "--output",
        type=Path,
        default=RESEARCH_COPYTRADE_00981A / "l1h9_slot_backtest_2026.json",
    )
    args = parser.parse_args(argv)

    cfg = SlotBacktestConfig(
        date_start=args.date_start,
        date_end=args.date_end,
        n_slots=9,
        hold_days=9,
        entry_price_mode="open",
        strategy_id=args.strategy_id,
        copytrade_batch_id=args.batch_id,
        source_summary=str(args.output),
    )

    conn = connect(args.db)
    try:
        summary = compute_copytrade_slot_summary(conn, etf_code=args.etf_code, cfg=cfg)
    finally:
        conn.close()

    if not summary:
        print("No copytrade_runs row found — run run_00981a_copytrade_backtest.py first", file=sys.stderr)
        return 1

    payload = build_summary_payload(
        track_id="00981a-l1h9",
        config=cfg,
        summary=summary,
        source_module="copytrade_backtest",
        extra={"run_id": summary.get("run_id")},
    )
    write_slot_backtest_summary(args.output, payload)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
