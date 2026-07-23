#!/usr/bin/env python3
"""Dual WMA 強勢回踩 · 進出場參數 sweep 回測。

Usage:
  PYTHONPATH=src python3 scripts/run_dual_wma_signal_backtest.py
  PYTHONPATH=src python3 scripts/run_dual_wma_signal_backtest.py --focus-signal imp_early_long
  PYTHONPATH=src python3 scripts/run_dual_wma_signal_backtest.py --phase exit
  PYTHONPATH=src python3 scripts/run_dual_wma_signal_backtest.py --phase w20-exit
  PYTHONPATH=src python3 scripts/run_dual_wma_signal_backtest.py --phase regime
  PYTHONPATH=src python3 scripts/run_dual_wma_signal_backtest.py --phase oos
  PYTHONPATH=src python3 scripts/run_dual_wma_signal_backtest.py --compare-all-signals
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_config import DEFAULT_ETF_CODES, parse_etf_codes
from project_dotenv import load_project_dotenv
from render_rrg_universe_html import _load_trading_dates_range
from report_paths import RESEARCH_RRG
from research.backtest.dual_wma_signal_backtest import (
    render_phase2_imp_early_long_md,
    render_phase2b_w20_leading_md,
    render_phase3_regime_md,
    render_phase4_oos_md,
    render_sweep_md,
    run_full_sweep,
    run_phase2_imp_early_long_exit_sweep,
    run_phase2b_w20_leading_tune,
    run_phase3_imp_early_long_regime_sweep,
    run_phase4_oos_holdout,
)
from stock_db import DEFAULT_DB_PATH, connect

DEFAULT_DATE_FROM = "2026-04-01"
DEFAULT_DATE_TO = "2026-07-03"


def main() -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="Dual WMA 訊號 sweep 回測")
    parser.add_argument("--date-from", default=DEFAULT_DATE_FROM)
    parser.add_argument("--date-to", default=DEFAULT_DATE_TO)
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--focus-signal",
        choices=("lead_pullback_buy", "imp_early_long", "all"),
        default="lead_pullback_buy",
        help="Sweep 主軸訊號（預設 lead_pullback_buy）",
    )
    parser.add_argument(
        "--compare-all-signals",
        action="store_true",
        help="同時比較四種預設情境訊號（等同 --focus-signal all）",
    )
    parser.add_argument(
        "--phase",
        choices=("entry", "exit", "w20-exit", "regime", "oos"),
        default="entry",
        help="entry · exit · w20-exit · regime · oos=Phase4 OOS holdout",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    focus_signal = "all" if args.compare_all_signals else args.focus_signal

    etf_codes = parse_etf_codes(args.etf_codes)
    conn = connect(args.db)
    try:
        dates = _load_trading_dates_range(
            conn, date_from=args.date_from, date_to=args.date_to
        )
        if not dates:
            raise ValueError(f"無交易日 {args.date_from} → {args.date_to}")

        t0 = time.perf_counter()
        if args.phase == "exit":
            payload = run_phase2_imp_early_long_exit_sweep(
                conn,
                dates=dates,
                etf_codes=etf_codes,
            )
        elif args.phase == "w20-exit":
            payload = run_phase2b_w20_leading_tune(
                conn,
                dates=dates,
                etf_codes=etf_codes,
            )
        elif args.phase == "regime":
            payload = run_phase3_imp_early_long_regime_sweep(
                conn,
                dates=dates,
                etf_codes=etf_codes,
            )
        elif args.phase == "oos":
            payload = run_phase4_oos_holdout(
                conn,
                etf_codes=etf_codes,
            )
        else:
            payload = run_full_sweep(
                conn,
                dates=dates,
                etf_codes=etf_codes,
                focus_signal=focus_signal,
            )
        elapsed = time.perf_counter() - t0
    finally:
        conn.close()

    if args.phase == "exit":
        md = render_phase2_imp_early_long_md(payload)
    elif args.phase == "w20-exit":
        md = render_phase2b_w20_leading_md(payload)
    elif args.phase == "regime":
        md = render_phase3_regime_md(payload)
    elif args.phase == "oos":
        md = render_phase4_oos_md(payload)
    else:
        md = render_sweep_md(payload)
    if args.output:
        out = args.output
    elif args.phase == "w20-exit":
        out = RESEARCH_RRG / f"{args.date_to.replace('-', '')}_imp_early_long_phase2b_w20_exit.md"
    elif args.phase == "regime":
        out = RESEARCH_RRG / f"{args.date_to.replace('-', '')}_imp_early_long_phase3_regime.md"
    elif args.phase == "oos":
        out = RESEARCH_RRG / f"{args.date_to.replace('-', '')}_imp_early_long_phase4_oos.md"
    elif args.phase == "exit":
        out = RESEARCH_RRG / f"{args.date_to.replace('-', '')}_imp_early_long_phase2_exit.md"
    elif focus_signal == "imp_early_long":
        out = RESEARCH_RRG / f"{args.date_to.replace('-', '')}_imp_early_long_sweep.md"
    else:
        out = RESEARCH_RRG / f"{args.date_to.replace('-', '')}_dual_wma_lead_pullback_sweep.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")

    top = payload["top_by_excess"][:3]
    label = (
        "phase4 oos"
        if args.phase == "oos"
        else (
            "phase3 regime"
            if args.phase == "regime"
            else (
                "phase2b w20-exit"
                if args.phase == "w20-exit"
                else ("phase2 exit" if args.phase == "exit" else f"sweep ({focus_signal})")
            )
        )
    )
    print(f"Dual WMA {label}: {args.date_from} → {args.date_to} · {elapsed:.1f}s")
    for i, r in enumerate(top, 1):
        if args.phase == "oos":
            print(
                f"  #{i} {r.get('window')} n={r.get('n')} excess={r.get('mean_excess_pct')}% "
                f"win={r.get('win_rate')}%"
            )
        elif args.phase == "regime":
            print(
                f"  #{i} {r.get('regime_filter')} n={r['n']} excess={r.get('mean_excess_pct')}% "
                f"win={r.get('win_rate')}%"
            )
        elif args.phase == "w20-exit":
            print(
                f"  #{i} {r.get('exit_label')} n={r['n']} excess={r.get('mean_excess_pct')}% "
                f"med={r.get('median_excess_pct')}% hold={r.get('mean_hold_polls')}"
            )
        elif args.phase == "exit":
            print(
                f"  #{i} exit={r.get('exit_rule')} n={r['n']} excess={r.get('mean_excess_pct')}% "
                f"med_excess={r.get('median_excess_pct')}% hold={r.get('mean_hold_polls')} "
                f"p10={r.get('p10_net_pct')}%"
            )
        elif focus_signal == "imp_early_long":
            print(
                f"  #{i} small_ext={r.get('small_extension_max')} aligned={r.get('aligned_max')} "
                f"both_vec={r.get('require_both_vectors')} w20={r.get('w20_rs_min')}–{r.get('w20_rs_max')} "
                f"exit={r.get('exit_rule')} n={r['n']} excess={r.get('mean_excess_pct')}% "
                f"win={r.get('win_rate')}%"
            )
        else:
            print(
                f"  #{i} pb={r.get('pullback_min')} max={r.get('pullback_max')} "
                f"w5imp={r.get('require_w5_improving')} exit={r.get('exit_rule')} "
                f"n={r['n']} excess={r.get('mean_excess_pct')}% win={r.get('win_rate')}%"
            )
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
