#!/usr/bin/env python3
"""Run unified cross-track backtest comparison (comparison layer only).

Usage:
  PYTHONPATH=src .venv/bin/python scripts/run_unified_backtest_comparison.py
  PYTHONPATH=src .venv/bin/python scripts/run_unified_backtest_comparison.py --window cmp_rolling_252d
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from backtest_standard_config import BacktestStandardConfig, load_backtest_standard_config  # noqa: E402
from research.backtest.unified_comparison import run_unified_comparison  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unified backtest comparison layer")
    parser.add_argument(
        "--window",
        default="cmp_max_common",
        help="window_id from config/backtest_standard.yaml",
    )
    parser.add_argument(
        "--no-cost",
        action="store_true",
        help="disable cost model (sensitivity)",
    )
    args = parser.parse_args(argv)

    cfg = load_backtest_standard_config()
    if args.no_cost:
        cost = dict(cfg.cost_model)
        cost["enabled"] = False
        cfg = BacktestStandardConfig(
            version=cfg.version,
            benchmark=cfg.benchmark,
            comparison_notional_ntd=cfg.comparison_notional_ntd,
            track_adapters=cfg.track_adapters,
            ensemble_teams=cfg.ensemble_teams,
            probe_radar=cfg.probe_radar,
            meta_router=cfg.meta_router,
            windows=cfg.windows,
            min_trading_days_for_annualization=cfg.min_trading_days_for_annualization,
            cost_model=cost,
            excess_definition=cfg.excess_definition,
            significance=cfg.significance,
            output=cfg.output,
        )

    print(f"Running unified comparison · window={args.window} · cost={cfg.cost_model.get('enabled')}")
    result = run_unified_comparison(window_id=args.window, cfg=cfg)
    print(f"Wrote {result['json_path']}")
    print(f"Wrote {result['md_path']}")
    for row in result["table"]["rows"]:
        print(
            f"  #{row['rank']} {row['strategy_id']}: "
            f"interval_excess={row.get('interval_excess_pct')}% · "
            f"sharpe={row.get('sharpe_ratio')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
