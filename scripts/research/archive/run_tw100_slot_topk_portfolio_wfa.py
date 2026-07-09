#!/usr/bin/env python3
"""TW100 slot-based Top-K portfolio WFA · Alpha158 + LightGBM · .venv-qlib."""

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

    from research.backtest.archive.tw100_alpha158_lgbm_wfa import DEFAULT_QLIB_URI  # noqa: WPS433
    from research.backtest.archive.tw100_slot_topk_portfolio_wfa import (  # noqa: WPS433
        Tw100SlotTopkWfaConfig,
        run_tw100_slot_topk_portfolio_wfa,
    )
    from research.backtest.archive.tw100_walk_forward import write_walk_forward_artifact  # noqa: WPS433
    from stock_db import DEFAULT_DB_PATH, connect  # noqa: WPS433

    p = argparse.ArgumentParser(description="TW100 slot Top-K portfolio WFA")
    p.add_argument("--provider-uri", type=Path, default=DEFAULT_QLIB_URI)
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default="2026-06-26")
    p.add_argument("--split-spec", default="tw100_wfa_504_100")
    p.add_argument("--max-folds", type=int, default=None)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--top-k-alt", type=int, default=20)
    p.add_argument("--benchmark", default="0050")
    p.add_argument("--no-cost", action="store_true", help="Disable TW fee/tax cost model")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or (REPORT_DIR / f"tw100_slot_topk{args.top_k}_portfolio_wfa_{stamp}.json")

    conn = connect(args.db)
    try:
        for k in sorted({args.top_k, args.top_k_alt}):
            cfg = Tw100SlotTopkWfaConfig(
                provider_uri=args.provider_uri,
                start_date=args.start,
                end_date=args.end,
                split_spec_id=args.split_spec,
                max_folds=args.max_folds,
                top_k=k,
                benchmark_id=args.benchmark,
                use_cost_model=not args.no_cost,
            )
            print(f"Running slot Top-{k} portfolio WFA · max_folds={cfg.max_folds or 'all'} …")
            payload = run_tw100_slot_topk_portfolio_wfa(cfg, conn)
            k_out = args.out or (REPORT_DIR / f"tw100_slot_topk{k}_portfolio_wfa_{stamp}.json")
            if args.out and k != args.top_k:
                k_out = args.out.with_name(f"{args.out.stem}_k{k}{args.out.suffix}")
            write_walk_forward_artifact(payload, k_out)

            slot = payload.get("slot_portfolio_oos") or {}
            cmp_ = payload.get("comparison_vs_benchmark") or {}
            print(f"Wrote {k_out}")
            print(
                f"  top-{k} total_ret={slot.get('total_return_pct')}% "
                f"sharpe={slot.get('sharpe_ratio')} max_dd={slot.get('max_drawdown_pct')}% "
                f"cagr={slot.get('cagr_pct')}%"
            )
            print(
                f"  vs {args.benchmark} excess_ret={cmp_.get('excess_total_return_pct')}% "
                f"n_trades={slot.get('n_trades')} avg_hold={slot.get('avg_hold_days')}d"
            )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
