#!/usr/bin/env python3
"""Leading Dip · RRG geometry screener · per-funnel-leg replace study.

  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_rrg_geometry_replace.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_rrg_geometry_replace.py --write-artifact
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.leading_dip_rrg_geometry_replace import (  # noqa: E402
    GEO_ID,
    render_md,
    run_geometry_replace_study,
)
from research.backtest.leading_dip_sleeve_validate import (  # noqa: E402
    OOS_CUT_DEFAULT,
    START_DEFAULT,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

DEFAULT_STEM = RESEARCH_RRG / "20260716_leading_dip_rrg_geometry_replace"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Replace Leading Dip funnel legs with RRG geometry screens"
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default=START_DEFAULT)
    ap.add_argument("--oos-cut", default=OOS_CUT_DEFAULT)
    ap.add_argument("--screen", default=GEO_ID, choices=["G_lin_below_yx", "G_wedge_right"])
    ap.add_argument("--write-artifact", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    con = connect(args.db)
    try:
        result = run_geometry_replace_study(
            con,
            start=args.start,
            oos_cut=args.oos_cut,
            screen_id=args.screen,
        )
    finally:
        con.close()

    md = render_md(result)
    print(md)
    print("VERDICT:", result["verdict"]["choice"], "·", result["verdict"]["prose"])

    if args.write_artifact:
        stem = args.out or DEFAULT_STEM
        stem.parent.mkdir(parents=True, exist_ok=True)
        # Strip heavy nested frames — stages already hold snaps
        path_json = Path(str(stem) + ".json")
        path_md = Path(str(stem) + ".md")
        path_json.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        path_md.write_text(md if md.endswith("\n") else md + "\n")
        print(f"wrote {path_md}")
        print(f"wrote {path_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
