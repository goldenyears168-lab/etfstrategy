#!/usr/bin/env python3
"""Pre-release conviction score → D+1 formal backtest report."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_improving_s3rv_executable import (  # noqa: E402
    render_pre_release_backtest_markdown,
    run_pre_release_backtest,
)

REPORT_DIR = ROOT / "reports" / "research" / "rrg"


def main() -> int:
    result = run_pre_release_backtest()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    json_path = REPORT_DIR / f"pre_release_score_backtest_{stamp}.json"
    md_path = REPORT_DIR / f"pre_release_score_backtest_{stamp}.md"
    payload = {
        "meta": result.meta,
        "model": result.model,
        "universe": result.universe,
        "tier_stats": [asdict(t) for t in result.tier_stats],
        "decile_stats": result.decile_stats,
        "rank_stats": result.rank_stats,
        "portfolio": result.portfolio,
        "oos_tier_stats": [asdict(t) for t in result.oos_tier_stats],
    }
    md = render_pre_release_backtest_markdown(result)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(md, encoding="utf-8")
    print(md)
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
