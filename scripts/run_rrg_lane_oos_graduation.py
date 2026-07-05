#!/usr/bin/env python3
"""RRG lane OOS graduation · G2/G3/G4 (P4).

Examples:
  PYTHONPATH=src .venv/bin/python scripts/run_rrg_lane_oos_graduation.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_lane_oos_graduation import (  # noqa: E402
    DEFAULT_OUT_DIR,
    DEFAULT_RULEFIT_PATH,
    LaneGraduationConfig,
    run_lane_oos_graduation,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG lane OOS graduation (P4)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--rulefit", type=Path, default=DEFAULT_RULEFIT_PATH)
    args = parser.parse_args(argv)

    cfg = LaneGraduationConfig(rulefit_path=args.rulefit)
    result = run_lane_oos_graduation(cfg, out_dir=args.out_dir)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(json.dumps(result["graduation_gates"], ensure_ascii=False, indent=2))
    for k, v in result.get("artifacts", {}).items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
