#!/usr/bin/env python3
"""Leading Dip · orthogonal 3-layer funnel suitability (效能 × 效率).

  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_orthogonal_compact.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_orthogonal_compact.py --write-artifact
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.leading_dip_orthogonal_compact import (  # noqa: E402
    render_md,
    run_orthogonal_compact_study,
)
from research.backtest.leading_dip_sleeve_validate import (  # noqa: E402
    OOS_CUT_DEFAULT,
    START_DEFAULT,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

DEFAULT_STEM = RESEARCH_RRG / "20260716_leading_dip_orthogonal_compact"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Leading Dip orthogonal compact funnel · 效能×效率"
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default=START_DEFAULT)
    ap.add_argument("--oos-cut", default=OOS_CUT_DEFAULT)
    ap.add_argument("--write-artifact", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    con = connect(args.db)
    try:
        result = run_orthogonal_compact_study(
            con, start=args.start, oos_cut=args.oos_cut
        )
    finally:
        con.close()

    md = render_md(result)
    print(md)
    print("VERDICT:", result["verdict"]["choice"])
    print(result["verdict"]["prose"])

    if args.write_artifact:
        stem = args.out or DEFAULT_STEM
        stem.parent.mkdir(parents=True, exist_ok=True)
        Path(str(stem) + ".json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        )
        Path(str(stem) + ".md").write_text(md if md.endswith("\n") else md + "\n")
        print(f"wrote {stem}.md")
        print(f"wrote {stem}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
