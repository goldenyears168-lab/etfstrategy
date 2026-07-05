#!/usr/bin/env python3
"""Market probe × all strategies · stability · day-type · correlation research.

  PYTHONPATH=src .venv/bin/python scripts/run_market_probe_strategy_research.py --backfill
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.market_probe_strategy_research import (  # noqa: E402
    DEFAULT_DATE_START,
    run_probe_strategy_research,
)
from stock_db import DEFAULT_DB_PATH  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe radar × strategy correlation research")
    parser.add_argument("--date-start", default=DEFAULT_DATE_START)
    parser.add_argument("--date-end", default=None)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "reports" / "research" / "market-probe",
    )
    parser.add_argument("--backfill", action="store_true", help="backfill 1m kbar before research")
    parser.add_argument(
        "--backfill-recent-days",
        type=int,
        default=None,
        help="if set with --backfill, only recent N days",
    )
    args = parser.parse_args(argv)

    if args.backfill:
        cmd = [
            sys.executable,
            str(ROOT / "scripts" / "backfill_probe_kbar.py"),
            "--start",
            args.date_start,
        ]
        if args.date_end:
            cmd.extend(["--end", args.date_end])
        if args.backfill_recent_days:
            cmd.extend(["--recent-days", str(args.backfill_recent_days)])
        print("Backfilling probe kbar…")
        subprocess.run(cmd, check=True, cwd=str(ROOT))

    print(f"Running probe×strategy research · {args.date_start} → {args.date_end or 'latest'}")
    result = run_probe_strategy_research(
        date_start=args.date_start,
        date_end=args.date_end,
        db_path=args.db,
        out_dir=args.out_dir,
    )
    payload = result["payload"]
    q = payload["quality_verdict"]
    wf = payload.get("walk_forward") or {}
    print(f"Probe days: {payload['n_probe_days']} · Strategies: {payload['n_strategies']}")
    print(
        f"Stable (target {q.get('target_pct_sufficient')}%): "
        f"{q['stable_enough_for_routing']} · min axis {q.get('min_axis_pct_sufficient')}%"
    )
    print(f"Walk-forward: {'PASS' if wf.get('walk_forward_passed') else 'FAIL'} · {wf.get('verdict', '')}")
    print()
    print("=== Day types ===")
    for k, v in sorted(
        payload["day_type_taxonomy"]["market_type_counts"].items(), key=lambda x: -x[1]
    ):
        print(f"  {k}: {v} days")
    print()
    print("=== Best strategy per market type ===")
    for r in payload["recommendations_per_market_type"]:
        print(
            f"  {r['market_type_label']}: {r['best_strategy_label']} "
            f"(excess={r.get('mean_excess_in_market')}%)"
        )
    print()
    print(f"Wrote {result['json_path']}")
    print(f"Wrote {result['md_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
