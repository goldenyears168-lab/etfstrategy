#!/usr/bin/env python3
"""RRG lane RuleFit / decision-tree discovery (P2).

Examples:
  PYTHONPATH=src .venv/bin/python scripts/run_rrg_lane_rulefit.py
  PYTHONPATH=src .venv/bin/python scripts/run_rrg_lane_rulefit.py --train-end 2023-12-31
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.archive.rrg_lane_rulefit import (  # noqa: E402
    DEFAULT_MISS_PATH,
    DEFAULT_OUT_DIR,
    DEFAULT_PANEL_PATH,
    run_rulefit_discovery,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG lane RuleFit discovery (P2)")
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL_PATH)
    parser.add_argument("--miss-list", type=Path, default=DEFAULT_MISS_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--train-end", default="2024-12-31")
    parser.add_argument("--label-threshold", type=float, default=3.0)
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--min-samples-leaf", type=int, default=15)
    args = parser.parse_args(argv)

    result = run_rulefit_discovery(
        panel_path=args.panel,
        miss_path=args.miss_list,
        out_dir=args.out_dir,
        train_end=args.train_end,
        label_threshold=args.label_threshold,
        max_depth=args.max_depth,
        min_samples_leaf=args.min_samples_leaf,
    )
    meta = result["meta"]
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print(
        f"raw={meta['n_raw_rules']} pass={meta['n_pass_gates']} "
        f"greedy={meta['n_greedy_selected']}"
    )
    for k, v in result.get("artifacts", {}).items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
