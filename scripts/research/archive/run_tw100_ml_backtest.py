#!/usr/bin/env python3
"""TW100 ML research backtest · baseline Rank IC (Research layer · manual)."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.archive.tw100_ml_backtest import (  # noqa: E402
    Tw100MlBacktestConfig,
    run_tw100_baseline_backtest,
    write_tw100_backtest_artifact,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

REPORT_DIR = ROOT / "reports" / "research" / "tw100"


def main() -> int:
    p = argparse.ArgumentParser(description="TW100 ML baseline backtest (momentum Rank IC skeleton)")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--snapshot-date", default="2026-06-26", help="TW100 universe snapshot (frozen for research)")
    p.add_argument("--start", default="2015-01-01", help="Panel start (Research SSOT window)")
    p.add_argument("--end", default=None, help="Panel end (default: latest bar in DB)")
    p.add_argument("--mom-window", type=int, default=20, help="Baseline momentum window")
    p.add_argument("--is-ratio", type=float, default=0.7, help="In-sample date ratio for IC split")
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="JSON artifact path (default: reports/research/tw100/tw100_ml_baseline_YYYYMMDD.json)",
    )
    args = p.parse_args()

    cfg = Tw100MlBacktestConfig(
        snapshot_date=args.snapshot_date,
        start_date=args.start,
        end_date=args.end,
        mom_window=args.mom_window,
        is_ratio=args.is_ratio,
    )
    stamp = date.today().strftime("%Y%m%d")
    out = args.out or (REPORT_DIR / f"tw100_ml_baseline_{stamp}.json")

    conn = connect(args.db)
    try:
        payload = run_tw100_baseline_backtest(conn, cfg)
    finally:
        conn.close()

    write_tw100_backtest_artifact(payload, out)
    ic = payload.get("ic") or {}
    print(f"Wrote {out}")
    print(
        f"IC mean={ic.get('mean')} icir={ic.get('icir')} | "
        f"OOS mean={ic.get('oos_mean')} n={ic.get('oos_n_days')}"
    )
    dq = payload.get("data_quality") or {}
    if dq.get("n_stocks_with_bars_lt_504"):
        print(
            f"WARN: {dq['n_stocks_with_bars_lt_504']} stocks with <504 bars in window "
            f"(backfill before full walk-forward)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
