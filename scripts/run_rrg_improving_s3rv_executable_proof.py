#!/usr/bin/env python3
"""S3 RV oracle proof + executable variant backtest."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_improving_s3rv_executable import (  # noqa: E402
    render_proof_markdown,
    run_executable_proof,
)

REPORT_DIR = ROOT / "reports" / "research" / "rrg"


def main() -> int:
    parser = argparse.ArgumentParser(description="S3 RV executable proof")
    args = parser.parse_args()
    _ = args

    result = run_executable_proof()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    json_path = REPORT_DIR / f"s3rv_executable_proof_{stamp}.json"
    md_path = REPORT_DIR / f"s3rv_executable_proof_{stamp}.md"
    md = render_proof_markdown(result)
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(md, encoding="utf-8")
    print(md)
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
