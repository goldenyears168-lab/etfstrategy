#!/usr/bin/env python3
"""TW100 walk-forward backtest skeleton (504+100+7 · momentum Rank IC)."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.archive.tw100_walk_forward import (  # noqa: E402
    Tw100WalkForwardConfig,
    run_tw100_walk_forward_backtest,
    write_walk_forward_artifact,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

REPORT_DIR = ROOT / "reports" / "research" / "tw100"


def main() -> int:
    p = argparse.ArgumentParser(description="TW100 walk-forward baseline (Research layer)")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--snapshot-date", default="2026-06-26")
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--split-spec", default="tw100_wfa_504_100")
    p.add_argument("--mom-window", type=int, default=20)
    p.add_argument("--max-folds", type=int, default=None, help="Limit folds (debug)")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    cfg = Tw100WalkForwardConfig(
        snapshot_date=args.snapshot_date,
        start_date=args.start,
        end_date=args.end,
        split_spec_id=args.split_spec,
        mom_window=args.mom_window,
        max_folds=args.max_folds,
    )
    stamp = date.today().strftime("%Y%m%d")
    out = args.out or (REPORT_DIR / f"tw100_wfa_baseline_{stamp}.json")

    conn = connect(args.db)
    try:
        payload = run_tw100_walk_forward_backtest(conn, cfg)
    finally:
        conn.close()

    write_walk_forward_artifact(payload, out)
    agg = payload.get("aggregate_test_ic") or {}
    print(f"Wrote {out}")
    print(
        f"folds={payload.get('n_folds')} test_ic mean={agg.get('mean')} icir={agg.get('icir')} "
        f"n={agg.get('n_days')} gate={'PASS' if payload.get('oos_gate_passed') else 'FAIL'}"
    )
    return 0 if payload.get("oos_gate_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
