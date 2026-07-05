#!/usr/bin/env python3
"""GeoAlpha Phase 1 · conditional IC by geometry phase φ."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.geoalpha_conditional_ic import (  # noqa: E402
    run_geoalpha_conditional_ic,
    write_geoalpha_conditional_ic_report,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="GeoAlpha conditional IC report")
    ap.add_argument("--date-from", default="2020-01-01", help="Daily panel start (default 2020-01-01)")
    ap.add_argument("--date-to", default=None, help="Daily panel end (default: latest)")
    ap.add_argument(
        "--intraday",
        action="store_true",
        help="Use intraday panel (default window 2026-04-01→2026-07-03)",
    )
    ap.add_argument("--intraday-from", default="2026-04-01")
    ap.add_argument("--intraday-to", default="2026-07-03")
    args = ap.parse_args()

    result = run_geoalpha_conditional_ic(
        date_from=args.date_from,
        date_to=args.date_to,
        intraday=args.intraday,
        intraday_from=args.intraday_from,
        intraday_to=args.intraday_to,
    )
    json_path, md_path = write_geoalpha_conditional_ic_report(result)

    overall = result.get("overall_ic", {})
    hyp = result.get("hypotheses", {})
    print(f"Mode: {'intraday' if args.intraday else 'daily'} · n={result['meta'].get('n_panel', 0)}")
    for score in ("s_struct", "s_alpha", "s_joint"):
        sc = overall.get(score, {})
        print(
            f"  {score}: IC drs={sc.get('drs_5d', {}).get('ic', 0):+.4f} · "
            f"ret={sc.get('ret_5d', {}).get('ic', 0):+.4f} · "
            f"excess={sc.get('excess', {}).get('ic', 0):+.4f}"
        )
    for hid in ("H-G1", "H-G2", "H-A1"):
        h = hyp.get(hid, {})
        print(f"  {hid}: {'PASS' if h.get('passed') else 'FAIL'}")
    print(f"\nWrote {json_path}\nWrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
