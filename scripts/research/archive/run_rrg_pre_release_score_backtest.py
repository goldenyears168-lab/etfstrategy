#!/usr/bin/env python3
"""Pre-release conviction score → D+1 formal backtest report."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.archive.rrg_pre_release_score_backtest import (  # noqa: E402
    render_backtest_markdown,
    run_pre_release_backtest,
)

REPORT_DIR = ROOT / "reports" / "research" / "rrg"


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-release score backtest")
    parser.add_argument("--oos-days", type=int, default=252)
    args = parser.parse_args()

    result = run_pre_release_backtest(oos_days=args.oos_days)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    json_path = REPORT_DIR / f"pre_release_score_backtest_{stamp}.json"
    md_path = REPORT_DIR / f"pre_release_score_backtest_{stamp}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_backtest_markdown(result), encoding="utf-8")
    print(render_backtest_markdown(result))
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
