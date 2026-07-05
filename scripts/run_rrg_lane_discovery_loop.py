#!/usr/bin/env python3
"""RRG lane discovery loop · case scan · miss-list · preregistered sweep.

Topic: config/research.yaml → rrg-lane-discovery-loop

Examples:
  PYTHONPATH=src .venv/bin/python scripts/run_rrg_lane_discovery_loop.py
  PYTHONPATH=src .venv/bin/python scripts/run_rrg_lane_discovery_loop.py --no-sweep
  PYTHONPATH=src .venv/bin/python scripts/run_research_sweep.py \\
    --topic rrg-lane-discovery-loop --family seed-grid --dry-run \\
    --runner-module research.backtest.rrg_lane_discovery_loop
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_lane_discovery_loop import (  # noqa: E402
    TOPIC_ID,
    _expand_seed_grid,
    load_discovery_topic,
    run_discovery_loop,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG lane discovery loop")
    parser.add_argument("--no-sweep", action="store_true", help="scan + miss-list only")
    parser.add_argument(
        "--cooccurrence-only",
        action="store_true",
        help="run co-occurrence + exclusion only (no streak scan / miss-list)",
    )
    parser.add_argument("--min-streak-pct", type=float, default=None)
    parser.add_argument("--miss-min-ret-3d", type=float, default=None)
    parser.add_argument("--dry-run-grid", action="store_true", help="print preregistered sweep grid")
    args = parser.parse_args(argv)

    if args.dry_run_grid:
        topic = load_discovery_topic()
        grid = _expand_seed_grid(topic["sweep"])
        print(f"topic={TOPIC_ID} cells={len(grid)}")
        for cell in grid[:20]:
            print(f"  {cell}")
        if len(grid) > 20:
            print(f"  ... +{len(grid) - 20} more")
        return 0

    kwargs: dict = {"run_sweep": not args.no_sweep}
    if args.min_streak_pct is not None:
        kwargs["min_streak_pct"] = args.min_streak_pct
    if args.miss_min_ret_3d is not None:
        kwargs["miss_min_ret_3d"] = args.miss_min_ret_3d
    if args.cooccurrence_only:
        kwargs["cooccurrence_only"] = True
        kwargs["run_sweep"] = True

    result = run_discovery_loop(**kwargs)
    print(json.dumps(result["meta"], ensure_ascii=False, indent=2))
    if args.cooccurrence_only:
        cs = result.get("cooccurrence", {}).get("cohort_summary", {})
        print(
            f"cooccurrence success={cs.get('n_success')} "
            f"exclusion={result.get('cooccurrence', {}).get('active_exclusion_tags')}"
        )
    else:
        print(f"cohort n={result['cohort_scan']['n_streaks']} miss={result['cohort_scan']['n_miss_strong']}")
    print(f"sweep pass={result['sweep']['n_pass_all']} greedy={len(result['sweep']['greedy_selected'])}")
    for k, v in result.get("artifacts", {}).items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
