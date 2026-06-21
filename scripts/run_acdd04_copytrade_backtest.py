#!/usr/bin/env python3
"""安聯台灣科技基金（ACDD04）月前十大跟單回測 → markdown 報告。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.mutual_fund_copytrade import (  # noqa: E402
    ACTION_FILTER_ALL_ADD,
    ACTION_FILTER_INITIATION,
    ACTION_FILTER_TOP3_INITIATION,
    ACTION_FILTERS,
    DISCLOSURE_METHODS,
    run_mutual_fund_copytrade_backtest,
    write_mutual_fund_copytrade_report,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402
from sync_mutual_fund_holdings import ALLIANZ_TW_TECH  # noqa: E402

DEFAULT_HORIZONS = (5, 9, 10, 15, 20, 30)


def _run_batch(
    conn,
    *,
    disclosure_method: str,
    action_filter: str,
    entry_lag_days: int,
    hold_trading_days: int,
    capital: float,
    cost_bps: float,
    window_start: str | None,
    window_end: str | None,
):
    return run_mutual_fund_copytrade_backtest(
        conn,
        ALLIANZ_TW_TECH,
        disclosure_method=disclosure_method,
        action_filter=action_filter,
        entry_lag_days=entry_lag_days,
        hold_trading_days=hold_trading_days,
        capital_ntd=capital,
        cost_bps=cost_bps,
        window_start=window_start,
        window_end=window_end,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ACDD04 月前十大跟單回測（公告日代理 · L1 進場）"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--cost-bps", type=float, default=20.0)
    parser.add_argument(
        "--disclosure",
        choices=sorted(DISCLOSURE_METHODS),
        default="lag28",
        help="公告日代理方法",
    )
    parser.add_argument(
        "--action-filter",
        choices=sorted(ACTION_FILTERS),
        default=ACTION_FILTER_ALL_ADD,
        help="訊號篩選",
    )
    parser.add_argument("--entry-lag", type=int, default=0, help="0=L1 公告隔天")
    parser.add_argument("--hold", type=int, default=20, help="持有交易日 H")
    parser.add_argument("--window-start", default=None)
    parser.add_argument("--window-end", default=None)
    parser.add_argument(
        "--compare-filters",
        action="store_true",
        default=True,
        help="附錄：all_add / initiation / top3_initiation 對照（預設開啟）",
    )
    parser.add_argument(
        "--no-compare-filters",
        action="store_false",
        dest="compare_filters",
        help="略過 filter 對照附錄",
    )
    parser.add_argument(
        "--compare-disclosure",
        action="store_true",
        help="附錄：公告日敏感性（四種代理）",
    )
    parser.add_argument(
        "--horizon-sweep",
        action="store_true",
        help=f"附錄：H 掃描 {DEFAULT_HORIZONS}",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="不寫入 reports/",
    )
    args = parser.parse_args(argv)

    conn = connect(args.db)
    common = dict(
        entry_lag_days=args.entry_lag,
        capital=args.capital,
        cost_bps=args.cost_bps,
        window_start=args.window_start,
        window_end=args.window_end,
    )

    primary = _run_batch(
        conn,
        disclosure_method=args.disclosure,
        action_filter=args.action_filter,
        hold_trading_days=args.hold,
        **common,
    )

    filter_rows = None
    if args.compare_filters:
        filter_rows = [
            _run_batch(
                conn,
                disclosure_method=args.disclosure,
                action_filter=f,
                hold_trading_days=args.hold,
                **common,
            )
            for f in (
                ACTION_FILTER_ALL_ADD,
                ACTION_FILTER_INITIATION,
                ACTION_FILTER_TOP3_INITIATION,
            )
        ]

    disclosure_rows = None
    if args.compare_disclosure:
        disclosure_rows = [
            _run_batch(
                conn,
                disclosure_method=m,
                action_filter=args.action_filter,
                hold_trading_days=args.hold,
                **common,
            )
            for m in sorted(DISCLOSURE_METHODS)
        ]

    horizon_rows = None
    if args.horizon_sweep:
        horizon_rows = [
            _run_batch(
                conn,
                disclosure_method=args.disclosure,
                action_filter=args.action_filter,
                hold_trading_days=h,
                **common,
            )
            for h in DEFAULT_HORIZONS
        ]

    print(
        f"{primary.fund_code} {primary.strategy_id} "
        f"filter={primary.action_filter} disclosure={primary.disclosure_method}"
    )
    print(
        f"  days={primary.n_complete_days}/{primary.n_signal_days} "
        f"legs={primary.n_legs} pnl={primary.total_pnl_ntd:+,.0f} "
        f"alpha={primary.total_alpha_ntd:+,.0f} "
        f"wr={primary.win_rate_gross_pct}%"
    )

    if not args.no_report:
        out = write_mutual_fund_copytrade_report(
            primary,
            filter_rows=filter_rows,
            disclosure_rows=disclosure_rows,
            horizon_rows=horizon_rows,
        )
        print(f"Report: {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
