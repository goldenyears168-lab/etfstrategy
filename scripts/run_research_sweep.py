#!/usr/bin/env python3
"""Template: preregistered research sweep via sweep_runner.

Example (dry-run grid for rrg-lens-score-swap topic):
  PYTHONPATH=src .venv/bin/python scripts/run_research_sweep.py \\
    --topic rrg-lens-score-swap \\
    --family smoke-grid \\
    --dry-run

Custom runner module must expose:
  def run_cell(params: dict) -> dict   # returns metrics dict
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.sweep_runner import load_topic, run_sweep_grid  # noqa: E402


def _default_grid(topic_id: str) -> list[dict[str, Any]]:
    """Placeholder grid — replace with topic-specific preregistration."""
    return [{"variant": "baseline", "topic": topic_id}]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research sweep runner template")
    parser.add_argument("--topic", required=True, help="config/research.yaml topic id")
    parser.add_argument("--family", required=True, help="trial family id (sweep batch name)")
    parser.add_argument(
        "--runner-module",
        default="research.backtest.rrg_lens_score_swap",
        help="Python module with run_cell(params) -> metrics",
    )
    parser.add_argument("--dry-run", action="store_true", help="print grid only")
    args = parser.parse_args(argv)

    topic = load_topic(args.topic)
    sweep = topic.sweep or {}
    grid = sweep.get("phase1") or sweep.get("grid") or _default_grid(args.topic)
    if isinstance(grid, dict):
        # expand simple sweep dict {key: [values]} → grid of combos
        import itertools

        keys = list(grid.keys())
        vals = [grid[k] if isinstance(grid[k], list) else [grid[k]] for k in keys]
        param_grid = [dict(zip(keys, combo)) for combo in itertools.product(*vals)]
    elif isinstance(grid, list):
        param_grid = grid
    else:
        param_grid = _default_grid(args.topic)

    print(f"topic={args.topic} family={args.family} cells={len(param_grid)}")
    if args.dry_run:
        for p in param_grid[:10]:
            print(f"  {p}")
        if len(param_grid) > 10:
            print(f"  ... +{len(param_grid) - 10} more")
        return 0

    mod = importlib.import_module(args.runner_module.replace("/", "."))
    if not hasattr(mod, "run_cell"):
        print(f"runner module {args.runner_module} missing run_cell(params)", file=sys.stderr)
        return 1

    hyp_ids = tuple(h.get("id", "") for h in topic.hypotheses if h.get("id"))
    runs = run_sweep_grid(
        topic,
        family_id=args.family,
        param_grid=param_grid,
        runner=mod.run_cell,
        hypothesis_ids=hyp_ids,
    )
    passed = sum(1 for r in runs if r.status == "pass")
    print(f"Done · {passed}/{len(runs)} pass · index at {topic.report_dir}/trials/{args.family}/_index.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
