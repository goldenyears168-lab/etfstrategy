#!/usr/bin/env python3
"""RRG Improving lifecycle monthly rule sweep · walk-forward validation.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/run_rrg_improving_lifecycle_monthly_sweep.py
  PYTHONPATH=src .venv/bin/python scripts/run_rrg_improving_lifecycle_monthly_sweep.py --test-days 22 --train-days 44
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_improving_lifecycle_rule_sweep import (  # noqa: E402
    render_sweep_markdown,
    run_rule_sweep,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG Improving lifecycle monthly rule sweep")
    parser.add_argument("--test-days", type=int, default=22, help="OOS test window (trading days)")
    parser.add_argument("--train-days", type=int, default=44, help="train window before test")
    parser.add_argument("--min-events", type=int, default=8, help="min events for train selection")
    parser.add_argument("--burst", type=float, default=5.0, help="burst threshold %%")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "reports" / "research" / "rrg",
        help="output directory",
    )
    args = parser.parse_args(argv)

    result = run_rule_sweep(
        test_days=args.test_days,
        train_days=args.train_days,
        min_events=args.min_events,
        burst_pct=args.burst,
    )
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    json_path = out_dir / f"improving_lifecycle_monthly_sweep_{stamp}.json"
    md_path = out_dir / f"improving_lifecycle_monthly_sweep_{stamp}.md"

    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_sweep_markdown(result), encoding="utf-8")

    print(render_sweep_markdown(result))
    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
