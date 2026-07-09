#!/usr/bin/env python3
"""C18acc × ABC v3+f1 · Phase1 fixed-weight partitioned NAV."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.c18acc_abc_dual_sleeve_phase1 import (  # noqa: E402
    build_phase1_from_comparison_json,
    render_c18acc_abc_dual_sleeve_phase1_md,
    run_c18acc_abc_dual_sleeve_phase1,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C18acc × ABC dual-sleeve Phase1 NAV")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2024-01-01")
    ap.add_argument("--date-end", default=None)
    ap.add_argument(
        "--from-comparison",
        type=Path,
        default=None,
        help="reuse comparison JSON (fast · linear weight combine)",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if args.from_comparison:
        payload = build_phase1_from_comparison_json(str(args.from_comparison))
    else:
        conn = connect(args.db)
        try:
            payload = run_c18acc_abc_dual_sleeve_phase1(
                conn,
                date_start=args.date_start,
                date_end=args.date_end,
            )
        finally:
            conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_abc_dual_sleeve_phase1.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_abc_dual_sleeve_phase1_md(payload), encoding="utf-8")

    print(payload.get("verdict", {}).get("summary", ""))
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
