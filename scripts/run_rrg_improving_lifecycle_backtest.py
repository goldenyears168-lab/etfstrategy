#!/usr/bin/env python3
"""RRG Improving lifecycle staged backtest.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/run_rrg_improving_lifecycle_backtest.py
  PYTHONPATH=src .venv/bin/python scripts/run_rrg_improving_lifecycle_backtest.py --burst 7
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_improving_lifecycle_backtest import (  # noqa: E402
    render_lifecycle_markdown,
    run_lifecycle_backtest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG Improving lifecycle backtest")
    parser.add_argument("--burst", type=float, default=5.0, help="burst threshold %% (default 5)")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "reports" / "research" / "rrg",
        help="output directory",
    )
    args = parser.parse_args(argv)

    result = run_lifecycle_backtest(burst_pct=args.burst)
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    json_path = out_dir / f"improving_lifecycle_backtest_{stamp}.json"
    md_path = out_dir / f"improving_lifecycle_backtest_{stamp}.md"

    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_lifecycle_markdown(result), encoding="utf-8")

    print(render_lifecycle_markdown(result))
    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
