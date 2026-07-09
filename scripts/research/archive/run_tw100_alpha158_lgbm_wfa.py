#!/usr/bin/env python3
"""TW100 Alpha158 + LightGBM walk-forward · use .venv-qlib (Python 3.11)."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_VENV = ROOT / ".venv-qlib"
REPORT_DIR = ROOT / "reports" / "research" / "tw100"


def _ensure_qlib_python() -> None:
    if sys.version_info >= (3, 13):
        print(
            "ERROR: 請用 .venv-qlib 執行：\n"
            f"  {DEFAULT_VENV / 'bin' / 'python'} {Path(__file__).name}",
            file=sys.stderr,
        )
        raise SystemExit(2)


def main() -> int:
    _ensure_qlib_python()

    from research.backtest.archive.tw100_alpha158_lgbm_wfa import (  # noqa: WPS433
        DEFAULT_QLIB_URI,
        QLIB_ALPHA158_LGBM_PRESET,
        Tw100Alpha158LgbmConfig,
        run_tw100_alpha158_lgbm_wfa,
        write_walk_forward_artifact,
    )

    preset = QLIB_ALPHA158_LGBM_PRESET
    p = argparse.ArgumentParser(description="TW100 Alpha158 + LightGBM walk-forward (Research)")
    p.add_argument("--provider-uri", type=Path, default=DEFAULT_QLIB_URI)
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--split-spec", default="tw100_wfa_504_100")
    p.add_argument("--max-folds", type=int, default=None, help="Limit folds (debug)")
    p.add_argument("--valid-ratio", type=float, default=0.2)
    p.add_argument("--learning-rate", type=float, default=float(preset["learning_rate"]))
    p.add_argument("--num-leaves", type=int, default=int(preset["num_leaves"]))
    p.add_argument("--max-depth", type=int, default=int(preset["max_depth"]))
    p.add_argument("--colsample-bytree", type=float, default=float(preset["colsample_bytree"]))
    p.add_argument("--subsample", type=float, default=float(preset["subsample"]))
    p.add_argument("--lambda-l1", type=float, default=float(preset["lambda_l1"]))
    p.add_argument("--lambda-l2", type=float, default=float(preset["lambda_l2"]))
    p.add_argument("--min-data-in-leaf", type=int, default=20)
    p.add_argument("--min-child-samples", type=int, default=20)
    p.add_argument("--num-boost-round", type=int, default=int(preset["num_boost_round"]))
    p.add_argument(
        "--early-stopping-rounds",
        type=int,
        default=int(preset["early_stopping_rounds"]),
        help="0 = disable early stopping",
    )
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    cfg = Tw100Alpha158LgbmConfig(
        provider_uri=args.provider_uri,
        start_date=args.start,
        end_date=args.end,
        split_spec_id=args.split_spec,
        max_folds=args.max_folds,
        valid_ratio=args.valid_ratio,
        learning_rate=args.learning_rate,
        num_leaves=args.num_leaves,
        max_depth=args.max_depth,
        colsample_bytree=args.colsample_bytree,
        subsample=args.subsample,
        lambda_l1=args.lambda_l1,
        lambda_l2=args.lambda_l2,
        min_data_in_leaf=args.min_data_in_leaf,
        min_child_samples=args.min_child_samples,
        num_boost_round=args.num_boost_round,
        early_stopping_rounds=args.early_stopping_rounds,
    )
    stamp = date.today().strftime("%Y%m%d")
    out = args.out or (REPORT_DIR / f"tw100_alpha158_lgbm_wfa_{stamp}.json")

    print(
        f"Running Alpha158+LightGBM WFA · max_folds={cfg.max_folds or 'all'} "
        f"lr={cfg.learning_rate} leaves={cfg.num_leaves} rounds={cfg.num_boost_round} …"
    )
    payload = run_tw100_alpha158_lgbm_wfa(cfg)
    write_walk_forward_artifact(payload, out)

    agg = payload.get("aggregate_test_ic") or {}
    print(f"Wrote {out}")
    print(
        f"folds={payload.get('n_folds')} best_iter_mean={payload.get('best_iteration_mean'):.1f} "
        f"test_ic mean={agg.get('mean')} icir={agg.get('icir')} n={agg.get('n_days')} "
        f"gate={'PASS' if payload.get('oos_gate_passed') else 'FAIL'}"
    )
    return 0 if payload.get("oos_gate_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
