#!/usr/bin/env python3
"""Pre-release v2 factor research · formal backtest report."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.archive.rrg_improving_pre_release_v2 import (  # noqa: E402
    render_v2_research_markdown,
    run_pre_release_v2_research,
)

REPORT_DIR = ROOT / "reports" / "research" / "rrg"


def main() -> int:
    result = run_pre_release_v2_research()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    json_path = REPORT_DIR / f"pre_release_v2_research_{stamp}.json"
    md_path = REPORT_DIR / f"pre_release_v2_research_{stamp}.md"
    payload = {
        "meta": result.meta,
        "hypotheses": result.hypotheses,
        "factor_lifts": [asdict(x) for x in result.factor_lifts],
        "loser_profile": result.loser_profile,
        "v1": result.v1,
        "v2": result.v2,
        "comparison": result.comparison,
    }
    md = render_v2_research_markdown(result)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(md, encoding="utf-8")
    print(md)
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
