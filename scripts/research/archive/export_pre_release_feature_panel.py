#!/usr/bin/env python3
"""Export pre-release feature panel (stock-day × atoms + ret_3d) for ML lane discovery.

Topic: reports/research/rrg-lane-foundation/research_plan.yaml · P1

Examples:
  PYTHONPATH=src .venv/bin/python scripts/export_pre_release_feature_panel.py
  PYTHONPATH=src .venv/bin/python scripts/export_pre_release_feature_panel.py \\
    --out reports/research/rrg/pre_release_feature_panel.parquet
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.archive.rrg_pre_release_funnel_lanes_3d import (  # noqa: E402
    export_pre_release_feature_panel,
)
from stock_db import PROJECT_ROOT  # noqa: E402

DEFAULT_OUT = (
    PROJECT_ROOT / "reports/research/rrg-lane-foundation/artifacts/pre_release_feature_panel.parquet"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export pre-release feature panel (P1)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output parquet/csv path")
    parser.add_argument("--format", choices=("parquet", "csv"), default="parquet")
    args = parser.parse_args(argv)

    result = export_pre_release_feature_panel(args.out, fmt=args.format)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"n_rows={result['n_rows']} atoms={len(result.get('atom_ids', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
