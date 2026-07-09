#!/usr/bin/env python3
"""S2 Setup core → next-day burst case study · 32 historical hits.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/run_rrg_s2_burst_case_study.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.archive.rrg_s2_burst_case_study import (  # noqa: E402
    render_s2_burst_case_markdown,
    run_s2_burst_case_study,
)


def main() -> int:
    result = run_s2_burst_case_study()
    out_dir = ROOT / "reports" / "research" / "rrg"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    json_path = out_dir / f"s2_to_burst_case_study_{stamp}.json"
    md_path = out_dir / f"s2_to_burst_case_study_{stamp}.md"

    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md = render_s2_burst_case_markdown(result)
    md_path.write_text(md, encoding="utf-8")

    print(md)
    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
