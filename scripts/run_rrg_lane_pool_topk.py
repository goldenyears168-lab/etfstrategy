#!/usr/bin/env python3
"""RRG pre-release pool Top-K · LightGBM walk-forward (P3 · H-ML-3).

Examples:
  PYTHONPATH=src .venv/bin/python scripts/run_rrg_lane_pool_topk.py
  PYTHONPATH=src .venv/bin/python scripts/run_rrg_lane_pool_topk.py --top-k 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_lane_pool_topk import (  # noqa: E402
    DEFAULT_OUT_DIR,
    DEFAULT_PANEL_PATH,
    PoolTopkConfig,
    run_pool_topk_wfa,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG pool Top-K LightGBM WFA (P3)")
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--split", default="walk_forward_3y1y")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--max-folds", type=int, default=None)
    args = parser.parse_args(argv)

    cfg = PoolTopkConfig(
        panel_path=args.panel,
        split_spec_id=args.split,
        top_k=args.top_k,
        max_folds=args.max_folds,
    )
    result = run_pool_topk_wfa(cfg, out_dir=args.out_dir)
    h = result["h_ml_3"]
    agg = result["aggregate_oos"]
    print(json.dumps({"h_ml_3": h, "aggregate_oos": agg}, ensure_ascii=False, indent=2))
    print(f"verdict={'PASS' if h.get('pass_all') else 'FAIL'}")
    for k, v in result.get("artifacts", {}).items():
        print(f"  {k}: {v}")
    return 0 if h.get("pass_all") else 1


if __name__ == "__main__":
    raise SystemExit(main())
