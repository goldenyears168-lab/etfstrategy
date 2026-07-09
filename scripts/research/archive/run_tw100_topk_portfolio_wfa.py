#!/usr/bin/env python3
"""TW100 Alpha158 + LightGBM Top-K portfolio walk-forward · use .venv-qlib."""

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

    from research.backtest.tw100_alpha158_lgbm_wfa import DEFAULT_QLIB_URI  # noqa: WPS433
    from research.backtest.tw100_topk_portfolio_wfa import (  # noqa: WPS433
        Tw100TopkPortfolioWfaConfig,
        run_tw100_topk_portfolio_wfa,
    )
    from research.backtest.tw100_walk_forward import write_walk_forward_artifact  # noqa: WPS433
    from stock_db import DEFAULT_DB_PATH, connect  # noqa: WPS433

    p = argparse.ArgumentParser(description="TW100 Top-K portfolio WFA (Alpha158+LightGBM)")
    p.add_argument("--provider-uri", type=Path, default=DEFAULT_QLIB_URI)
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default="2026-06-26")
    p.add_argument("--split-spec", default="tw100_wfa_504_100")
    p.add_argument("--max-folds", type=int, default=None)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--top-k-alt", type=int, default=20, help="Also report second K in output filename suffix")
    p.add_argument("--benchmark", default="0050")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    stamp = date.today().strftime("%Y%m%d")
    results: list[tuple[int, dict]] = []

    conn = connect(args.db)
    try:
        for k in sorted({args.top_k, args.top_k_alt}):
            cfg = Tw100TopkPortfolioWfaConfig(
                provider_uri=args.provider_uri,
                start_date=args.start,
                end_date=args.end,
                split_spec_id=args.split_spec,
                max_folds=args.max_folds,
                top_k=k,
                benchmark_id=args.benchmark,
            )
            print(f"Running Top-{k} portfolio WFA · max_folds={cfg.max_folds or 'all'} …")
            payload = run_tw100_topk_portfolio_wfa(cfg, conn)
            out = args.out or (REPORT_DIR / f"tw100_topk{k}_portfolio_wfa_{stamp}.json")
            if args.out and k != args.top_k:
                out = args.out.with_name(f"{args.out.stem}_k{k}{args.out.suffix}")
            write_walk_forward_artifact(payload, out)
            results.append((k, payload))
            port0 = (payload.get("portfolio_oos") or {}).get("cost_0bps") or {}
            port10 = (payload.get("portfolio_oos") or {}).get("cost_10bps") or {}
            print(f"Wrote {out}")
            print(
                f"  top-{k} OOS total_ret={port0.get('total_return')} "
                f"sharpe={port0.get('sharpe')} excess_vs_{args.benchmark}="
                f"{port0.get('mean_excess_vs_benchmark')} "
                f"| cost10bps total_ret={port10.get('total_return')}"
            )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
