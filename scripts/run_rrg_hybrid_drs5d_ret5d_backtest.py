#!/usr/bin/env python3
"""Hybrid WMA20 drs_5d + WMA5 extension + ret_5d separate track."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_hybrid_drs5d_ret5d_backtest import (  # noqa: E402
    run_hybrid_ret5d_research,
    write_hybrid_ret5d_report,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--skip-intraday",
        action="store_true",
        help="Skip intraday extension build (faster; no hybrid+intraday section)",
    )
    args = ap.parse_args()
    result = run_hybrid_ret5d_research(skip_intraday=args.skip_intraday)
    json_path, md_path = write_hybrid_ret5d_report(result)
    ic = result["ic_matrix"]
    v = result["verdict"]
    wfa = result.get("walk_forward_ret5d", {}).get("summary", {})
    print(
        f"struct IC drs={ic['struct_vs_drs5d']['ic']:+.4f} · "
        f"hybrid IC drs={ic['hybrid_vs_drs5d']['ic']:+.4f} · "
        f"ret5d IC price={ic['ret5d_vs_ret5d']['ic']:+.4f} · "
        f"WFA ret5d mean OOS={wfa.get('mean_oos_ic', 0):+.4f}"
    )
    print(f"Verdict: {v.get('summary', '')}")
    print(f"\nWrote {json_path}\nWrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
