#!/usr/bin/env python3
"""Fair compare · Leading Dip ↔ ABC v3+F1 (Research only).

  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_vs_abc_fair_compare.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_vs_abc_fair_compare.py --collect-abc
  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_vs_abc_fair_compare.py --arm-b-only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.leading_dip_vs_abc_fair_compare import (  # noqa: E402
    format_fair_tables,
    render_fair_compare_md,
    run_fair_compare,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

DEFAULT_DUMP = RESEARCH_RRG / "_cache" / "abc_f1_fair_events_2025.json"
DEFAULT_STEM = RESEARCH_RRG / "20260715_leading_dip_vs_abc_fair_compare"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fair compare Leading Dip vs ABC v3+F1")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default="2025-01-02")
    ap.add_argument("--oos-cut", default="2025-10-01")
    ap.add_argument("--abc-dump", type=Path, default=DEFAULT_DUMP)
    ap.add_argument(
        "--collect-abc",
        action="store_true",
        help="Chunk-build dual-WMA panel + dump ABC f1 events (~1h)",
    )
    ap.add_argument("--chunk-days", type=int, default=25)
    ap.add_argument("--arm-a-only", action="store_true")
    ap.add_argument("--arm-b-only", action="store_true")
    ap.add_argument("--out", type=Path, default=DEFAULT_STEM)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    con = connect(args.db)
    try:
        payload = run_fair_compare(
            con,
            start=args.start,
            oos_cut=args.oos_cut,
            abc_dump=args.abc_dump,
            collect_abc=args.collect_abc,
            chunk_days=args.chunk_days,
            skip_arm_a=bool(args.arm_b_only),
            skip_arm_b=bool(args.arm_a_only),
        )
    finally:
        con.close()

    payload["artifact_stem"] = str(args.out)
    print(format_fair_tables(payload))
    print()

    if not args.no_write:
        out_json = args.out.with_suffix(".json") if args.out.suffix != ".json" else args.out
        if args.out.suffix in (".md", ".json"):
            stem = args.out.with_suffix("")
        else:
            stem = args.out
        out_json = Path(str(stem) + ".json")
        out_md = Path(str(stem) + ".md")
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        out_md.write_text(render_fair_compare_md(payload), encoding="utf-8")
        print(f"Wrote {out_json}")
        print(f"Wrote {out_md}")

    # exit 0 even with ABC blocker if Arm B produced numbers (scaffold delivered)
    arm_b = payload.get("arm_b") or {}
    arm_a = payload.get("arm_a") or {}
    has_b = int((arm_b.get("B0") or {}).get("n") or 0) > 0
    has_a = int((arm_a.get("A0") or {}).get("n") or 0) > 0
    if not has_a and not has_b and payload.get("blockers"):
        print("FAIL: no arm produced events", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
