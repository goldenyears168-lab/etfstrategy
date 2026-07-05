#!/usr/bin/env python3
"""TW100 monthly WFA · next-month excess return · LightGBM or momentum baseline."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_VENV = ROOT / ".venv-qlib"
REPORT_DIR = ROOT / "reports" / "research" / "tw100"


def _ensure_lightgbm_if_needed(signal: str) -> None:
    if signal != "lightgbm":
        return
    if sys.version_info >= (3, 13):
        print(
            "ERROR: LightGBM 請用 .venv-qlib 執行：\n"
            f"  {DEFAULT_VENV / 'bin' / 'python'} {Path(__file__).name}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        import lightgbm  # noqa: F401
    except ImportError as exc:
        print(
            f"ERROR: lightgbm not installed ({exc}). Use:\n"
            f"  {DEFAULT_VENV / 'bin' / 'python'} {Path(__file__).name}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


def main() -> int:
    from research.backtest.tw100_monthly_wfa import (  # noqa: WPS433
        Tw100MonthlyWfaConfig,
        run_tw100_monthly_wfa,
        write_walk_forward_artifact,
    )
    from stock_db import DEFAULT_DB_PATH, connect  # noqa: WPS433

    p = argparse.ArgumentParser(description="TW100 monthly WFA (next-month excess return)")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--snapshot-date", default="2026-06-26")
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--split-spec", default="tw100_monthly_wfa_36_6")
    p.add_argument(
        "--signal",
        choices=("lightgbm", "momentum_126d"),
        default="lightgbm",
        help="lightgbm=multi-feature · momentum_126d=6M baseline",
    )
    p.add_argument(
        "--feature-set",
        choices=("mom", "fund", "chip", "mom_fund", "mom_chip", "mom_fund_chip"),
        default="mom",
        help="mom | fund | chip | mom_fund | mom_chip | mom_fund_chip",
    )
    p.add_argument("--max-folds", type=int, default=None, help="Limit folds (debug)")
    p.add_argument("--benchmark", default="IX0001")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    _ensure_lightgbm_if_needed(args.signal)

    cfg = Tw100MonthlyWfaConfig(
        snapshot_date=args.snapshot_date,
        start_date=args.start,
        end_date=args.end,
        split_spec_id=args.split_spec,
        benchmark_code=args.benchmark,
        max_folds=args.max_folds,
        signal=args.signal,
        feature_set=args.feature_set,
    )
    stamp = date.today().strftime("%Y%m%d")
    suffix = "lgbm" if args.signal == "lightgbm" else "mom126"
    if args.feature_set != "mom":
        suffix = f"{suffix}_{args.feature_set}"
    out = args.out or (REPORT_DIR / f"tw100_monthly_wfa_{suffix}_{stamp}.json")

    print(
        f"Running monthly WFA · signal={args.signal} feature_set={args.feature_set} "
        f"· max_folds={cfg.max_folds or 'all'} …"
    )
    conn = connect(args.db)
    try:
        payload = run_tw100_monthly_wfa(conn, cfg)
    finally:
        conn.close()

    write_walk_forward_artifact(payload, out)
    agg = payload.get("aggregate_test_ic") or {}
    print(f"Wrote {out}")
    print(
        f"folds={payload.get('n_folds')} test_ic mean={agg.get('mean')} icir={agg.get('icir')} "
        f"n_months={agg.get('n_days')} gate={'PASS' if payload.get('oos_gate_passed') else 'FAIL'}"
    )
    return 0 if payload.get("oos_gate_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
