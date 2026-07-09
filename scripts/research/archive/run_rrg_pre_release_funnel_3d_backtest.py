#!/usr/bin/env python3
"""RRG pre-release funnel lanes · 3-day hold backtest runner."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.archive.rrg_pre_release_funnel_lanes_3d import (  # noqa: E402
    CONFIG_PATH,
    render_funnel_3d_markdown,
    run_funnel_3d_backtest,
    write_validated_coverage_3d_to_config,
)

REPORT_DIR = ROOT / "reports" / "research" / "rrg"


def main() -> int:
    ap = argparse.ArgumentParser(description="RRG pre-release funnel lane · 3-day hold backtest")
    ap.add_argument("--no-discovery", action="store_true", help="Use coverage lanes from YAML only")
    ap.add_argument(
        "--write-coverage",
        action="store_true",
        help="Write discovered coverage lanes back to config SSOT",
    )
    ap.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = ap.parse_args()

    result = run_funnel_3d_backtest(
        config_path=args.config,
        run_discovery=not args.no_discovery,
    )

    if args.write_coverage and result.get("coverage_defs"):
        write_validated_coverage_3d_to_config(result["coverage_defs"], args.config)
        print(f"Updated coverage lanes in {args.config}")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    json_path = REPORT_DIR / f"pre_release_funnel_lanes_3d_{stamp}.json"
    md_path = REPORT_DIR / f"pre_release_funnel_lanes_3d_{stamp}.md"

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    md = render_funnel_3d_markdown(result, cfg)

    payload = {
        "meta": result["meta"],
        "config": result["config"],
        "pool_baseline": result["pool_baseline"],
        "strict_lanes": result["strict_lanes"],
        "coverage_lanes": result["coverage_lanes"],
        "union_strict": result["union_strict"],
        "union_all": result["union_all"],
        "jaccard_matrix": result["jaccard_matrix"],
        "coverage_defs": result.get("coverage_defs", []),
        "june2026_cases": result.get("june2026_cases"),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(md, encoding="utf-8")

    print(md)
    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
