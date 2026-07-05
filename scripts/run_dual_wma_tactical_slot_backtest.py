#!/usr/bin/env python3
"""Dual WMA hook_buy / imp_early_long · 戰術槽與 C18acc 組合回測。

Usage:
  PYTHONPATH=src python3 scripts/run_dual_wma_tactical_slot_backtest.py
  PYTHONPATH=src python3 scripts/run_dual_wma_tactical_slot_backtest.py --phase imp-combo \\
    --date-from 2025-07-01 --date-to 2026-07-03
  PYTHONPATH=src python3 scripts/run_dual_wma_tactical_slot_backtest.py --phase graduation \\
    --date-from 2025-07-01 --date-to 2026-07-03
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from project_config import DEFAULT_ETF_CODES, parse_etf_codes
from project_dotenv import load_project_dotenv
from render_rrg_universe_html import _load_trading_dates_range
from report_paths import RESEARCH_RRG
from research.backtest.dual_wma_signal_backtest import run_phase4_oos_holdout
from research.backtest.dual_wma_tactical_slot_backtest import (
    HookEntryParams,
    TacticalExitParams,
    merge_graduation_verdict,
    render_graduation_verdict_md,
    render_hook_entry_sweep_md,
    render_hook_pool_audit_md,
    render_hook_pool_tier_sweep_md,
    render_imp_early_long_tactical_slot_md,
    render_tactical_slot_md,
    run_hook_entry_sweep,
    run_hook_entry_sweep_1b,
    run_hook_pool_audit,
    run_hook_pool_tier_sweep,
    run_imp_early_long_tactical_slot_study,
    run_tactical_slot_study,
)
from stock_db import DEFAULT_DB_PATH, connect

DEFAULT_DATE_FROM = "2026-04-01"
DEFAULT_DATE_TO = "2026-07-03"
IMP_COMBO_DATE_FROM = "2025-07-01"


def main() -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="Dual WMA hook_buy 戰術槽 / Phase1 sweep")
    parser.add_argument("--date-from", default=DEFAULT_DATE_FROM)
    parser.add_argument("--date-to", default=DEFAULT_DATE_TO)
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--phase",
        choices=("combo", "entry-sweep", "entry-sweep-1b", "pool-audit", "pool-sweep", "imp-combo", "graduation"),
        default="combo",
        help="combo · imp-combo · graduation · entry-sweep · …",
    )
    parser.add_argument("--capital", type=float, default=50_000.0)
    parser.add_argument("--c18acc-slots", type=int, default=2)
    parser.add_argument("--tactical-slots", type=int, default=1)
    parser.add_argument("--retrace-eps", type=float, default=0.10)
    parser.add_argument("--peak-signal-min", type=float, default=1.0)
    parser.add_argument("--allow-mixed-peak", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--hook-quality-min", type=float, default=0.0)
    parser.add_argument("--exit-cap-days", type=int, default=3)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    etf_codes = parse_etf_codes(args.etf_codes)
    conn = connect(args.db)
    combo_from = (
        IMP_COMBO_DATE_FROM
        if args.phase in ("imp-combo", "graduation") and args.date_from == DEFAULT_DATE_FROM
        else args.date_from
    )
    try:
        if args.phase == "graduation":
            t0 = time.perf_counter()
            oos_payload = run_phase4_oos_holdout(conn, etf_codes=etf_codes)
            dates = _load_trading_dates_range(
                conn, date_from=combo_from, date_to=args.date_to
            )
            if not dates:
                raise ValueError(f"無交易日 {combo_from} → {args.date_to}")
            combo_payload = run_imp_early_long_tactical_slot_study(
                conn,
                dates=dates,
                etf_codes=etf_codes,
                total_capital=args.capital,
                c18acc_slots=args.c18acc_slots,
                tactical_slots=args.tactical_slots,
            )
            verdict = merge_graduation_verdict(
                g2=oos_payload.get("g2_oos_holdout") or {},
                g3=combo_payload.get("graduation") or {},
            )
            payload = {
                "oos": oos_payload,
                "combo": combo_payload,
                "verdict": verdict,
            }
            elapsed = time.perf_counter() - t0
        else:
            dates = _load_trading_dates_range(
                conn, date_from=args.date_from, date_to=args.date_to
            )
            if not dates and args.phase != "imp-combo":
                raise ValueError(f"無交易日 {args.date_from} → {args.date_to}")

            t0 = time.perf_counter()
            exit_params = TacticalExitParams(cap_days=args.exit_cap_days)
            if args.phase == "entry-sweep":
                payload = run_hook_entry_sweep(
                    conn, dates=dates, etf_codes=etf_codes, exit_params=exit_params
                )
            elif args.phase == "entry-sweep-1b":
                payload = run_hook_entry_sweep_1b(
                    conn, dates=dates, etf_codes=etf_codes, exit_params=exit_params
                )
            elif args.phase == "pool-audit":
                payload = run_hook_pool_audit(
                    conn, dates=dates, etf_codes=etf_codes
                )
            elif args.phase == "pool-sweep":
                payload = run_hook_pool_tier_sweep(
                    conn, dates=dates, etf_codes=etf_codes, exit_params=exit_params
                )
            elif args.phase == "imp-combo":
                dates = _load_trading_dates_range(
                    conn, date_from=combo_from, date_to=args.date_to
                )
                if not dates:
                    raise ValueError(f"無交易日 {combo_from} → {args.date_to}")
                payload = run_imp_early_long_tactical_slot_study(
                    conn,
                    dates=dates,
                    etf_codes=etf_codes,
                    total_capital=args.capital,
                    c18acc_slots=args.c18acc_slots,
                    tactical_slots=args.tactical_slots,
                )
            else:
                payload = run_tactical_slot_study(
                    conn,
                    dates=dates,
                    etf_codes=etf_codes,
                    total_capital=args.capital,
                    c18acc_slots=args.c18acc_slots,
                    tactical_slots=args.tactical_slots,
                    hook_entry=HookEntryParams(
                        retrace_eps=args.retrace_eps,
                        hook_quality_min=args.hook_quality_min,
                        peak_signal_min=args.peak_signal_min,
                        allow_mixed_peak=args.allow_mixed_peak,
                    ),
                    hook_exit=exit_params,
                )
            elapsed = time.perf_counter() - t0
    finally:
        conn.close()

    stem = args.date_to.replace("-", "")
    if args.phase == "graduation":
        md = render_graduation_verdict_md(
            oos_payload=payload["oos"],
            combo_payload=payload["combo"],
            verdict=payload["verdict"],
        )
        out = args.output or (RESEARCH_RRG / f"{stem}_imp_early_long_graduation.md")
        json_path = args.json or (RESEARCH_RRG / f"{stem}_imp_early_long_graduation.json")
        serializable = {
            "verdict": payload["verdict"],
            "g2_oos_holdout": payload["oos"].get("g2_oos_holdout"),
            "g3_graduation": payload["combo"].get("graduation"),
            "oos_rows": payload["oos"].get("rows"),
            "combo_summary": payload["combo"]["tactical_events"]["summary"],
            "portfolios": {
                k: v
                for k, v in payload["combo"]["portfolios"].items()
                if k != "shared_3slot_priority"
            },
        }
    elif args.phase == "imp-combo":
        md = render_imp_early_long_tactical_slot_md(payload)
        out = args.output or (RESEARCH_RRG / f"{stem}_imp_early_long_tactical_slot.md")
        json_path = args.json or (RESEARCH_RRG / f"{stem}_imp_early_long_tactical_slot.json")
        serializable = {
            k: v
            for k, v in payload.items()
            if k not in ("tactical_events",)
        }
        serializable["tactical_summary"] = payload["tactical_events"]["summary"]
        serializable["tactical_n_trades"] = len(payload["tactical_events"]["trades"])
    elif args.phase == "entry-sweep":
        md = render_hook_entry_sweep_md(payload)
        out = args.output or (RESEARCH_RRG / f"{stem}_hook_buy_phase1_entry_sweep.md")
        json_path = args.json or (RESEARCH_RRG / f"{stem}_hook_buy_phase1_entry_sweep.json")
        serializable = {
            "date_from": payload.get("date_from"),
            "date_to": payload.get("date_to"),
            "exit_cap_days": payload.get("exit_cap_days"),
            "imp_early_long_baseline": payload.get("imp_early_long_baseline"),
            "champion_hook_row": payload.get("champion_hook_row"),
            "by_retrace_eps": payload.get("by_retrace_eps"),
            "top_by_excess": payload.get("top_by_excess"),
            "n_grid": len(payload.get("rows") or []),
        }
    elif args.phase == "entry-sweep-1b":
        md = render_hook_entry_sweep_md(payload)
        out = args.output or (RESEARCH_RRG / f"{stem}_hook_buy_phase1b_entry_sweep.md")
        json_path = args.json or (RESEARCH_RRG / f"{stem}_hook_buy_phase1b_entry_sweep.json")
        serializable = {
            "phase": payload.get("phase"),
            "date_from": payload.get("date_from"),
            "date_to": payload.get("date_to"),
            "imp_early_long_baseline": payload.get("imp_early_long_baseline"),
            "champion_hook_row": payload.get("champion_hook_row"),
            "by_peak_signal_min": payload.get("by_peak_signal_min"),
            "top_by_n": payload.get("top_by_n"),
            "top_by_excess": payload.get("top_by_excess"),
            "n_grid": len(payload.get("rows") or []),
        }
    elif args.phase == "pool-audit":
        md = render_hook_pool_audit_md(payload)
        out = args.output or (RESEARCH_RRG / f"{stem}_hook_pool_audit.md")
        json_path = args.json or (RESEARCH_RRG / f"{stem}_hook_pool_audit.json")
        serializable = {
            k: v
            for k, v in payload.items()
            if k != "panel_meta"
        }
    elif args.phase == "pool-sweep":
        md = render_hook_pool_tier_sweep_md(payload)
        out = args.output or (RESEARCH_RRG / f"{stem}_hook_pool_tier_excess_sweep.md")
        json_path = args.json or (RESEARCH_RRG / f"{stem}_hook_pool_tier_excess_sweep.json")
        serializable = {
            "date_from": payload.get("date_from"),
            "date_to": payload.get("date_to"),
            "exit_cap_days": payload.get("exit_cap_days"),
            "imp_early_long_baseline": payload.get("imp_early_long_baseline"),
            "by_pool_tier": payload.get("by_pool_tier"),
            "top_by_excess_min_n": payload.get("top_by_excess_min_n"),
            "top_by_n_pos_excess": payload.get("top_by_n_pos_excess"),
            "n_eligible": payload.get("n_eligible"),
            "n_grid": payload.get("n_grid"),
        }
    else:
        md = render_tactical_slot_md(payload)
        out = args.output or (RESEARCH_RRG / f"{stem}_dual_wma_tactical_slot.md")
        json_path = args.json or (RESEARCH_RRG / f"{stem}_dual_wma_tactical_slot.json")
        serializable = {
            k: v
            for k, v in payload.items()
            if k not in ("hook_events",)
        }
        serializable["hook_summary"] = payload["hook_events"]["summary"]
        serializable["hook_n_trades"] = len(payload["hook_events"]["trades"])

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    json_path.write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    if args.phase == "graduation":
        v = payload["verdict"]
        g2 = payload["oos"]["g2_oos_holdout"]
        tac = payload["combo"]["tactical_events"]["summary"]
        part = payload["combo"]["portfolios"]["partitioned_2plus1"]
        print(
            f"imp_early_long graduation: combo {combo_from} → {args.date_to} · {elapsed:.1f}s"
        )
        print(f"  G2 OOS: {'PASS' if g2.get('passed') else 'FAIL'}")
        for r in payload["oos"].get("oos") or []:
            print(
                f"    {r.get('window')}: n={r.get('n')} excess={r.get('mean_excess_pct')}%"
            )
        print(
            f"  G3 combo: n={tac.get('n')} excess={tac.get('mean_excess_pct')}% "
            f"ρ={part.get('daily_return_corr')}"
        )
        print(f"  VERDICT: {v.get('verdict')} · {v.get('action')}")
    elif args.phase == "imp-combo":
        tac = payload["tactical_events"]["summary"]
        part = payload["portfolios"]["partitioned_2plus1"]
        base = payload["portfolios"]["c18acc_3slot_baseline"]
        grad = payload.get("graduation") or {}
        print(
            f"imp_early_long tactical slot: {payload['date_from']} → {payload['date_to']} · "
            f"{elapsed:.1f}s"
        )
        print(
            f"  imp n={tac.get('n')} excess={tac.get('mean_excess_pct')}% "
            f"overlap={payload.get('overlap_pct')}%"
        )
        print(
            f"  C18acc 3槽 CAGR={base.get('cagr_pct')}% → Partitioned {part.get('cagr_pct')}% "
            f"ρ={part.get('daily_return_corr')}"
        )
        print(f"  G3: {'PASS' if grad.get('g3_combo_pass') else 'FAIL'}")
    elif args.phase == "entry-sweep":
        imp = payload["imp_early_long_baseline"]
        top = (payload.get("top_by_excess") or [{}])[0]
        print(
            f"hook_buy Phase1 sweep: {args.date_from} → {args.date_to} · {elapsed:.1f}s"
        )
        print(
            f"  imp_early_long n={imp.get('n')} excess={imp.get('mean_excess_pct')}%"
        )
        print(
            f"  best hook retrace={top.get('retrace_eps')} hq={top.get('hook_quality_min')} "
            f"n={top.get('n')} excess={top.get('mean_excess_pct')}%"
        )
        for row in payload.get("by_retrace_eps") or []:
            print(
                f"  eps={row.get('retrace_eps')}: max_n={row.get('max_n')} "
                f"best_excess={row.get('best_mean_excess_pct')}%"
            )
    elif args.phase == "entry-sweep-1b":
        imp = payload["imp_early_long_baseline"]
        top_n = (payload.get("top_by_n") or [{}])[0]
        top_ex = (payload.get("top_by_excess") or [{}])[0]
        print(
            f"hook_buy Phase1b sweep: {args.date_from} → {args.date_to} · {elapsed:.1f}s"
        )
        print(
            f"  imp_early_long n={imp.get('n')} excess={imp.get('mean_excess_pct')}%"
        )
        print(
            f"  best n: peak={top_n.get('peak_signal_min')} mixed={top_n.get('allow_mixed_peak')} "
            f"n={top_n.get('n')} excess={top_n.get('mean_excess_pct')}%"
        )
        print(
            f"  best excess: peak={top_ex.get('peak_signal_min')} n={top_ex.get('n')} "
            f"excess={top_ex.get('mean_excess_pct')}%"
        )
        for row in payload.get("by_peak_signal_min") or []:
            print(
                f"  peak={row.get('peak_signal_min')} mixed={row.get('allow_mixed_peak')}: "
                f"max_n={row.get('max_n')} best_excess={row.get('best_mean_excess_pct')}%"
            )
    elif args.phase == "pool-audit":
        print(
            f"hook pool audit: {args.date_from} → {args.date_to} · {elapsed:.1f}s"
        )
        for row in payload.get("funnel") or []:
            print(
                f"  {row.get('tier')}: n={row.get('n_first_per_day')} "
                f"days={row.get('days_with_events')} mean/day={row.get('mean_per_day')}"
            )
        print(
            f"  stock-days candidate={payload.get('stock_days_with_candidate')} "
            f"raw_polls={payload.get('raw_candidate_polls')} "
            f"strict_champ={payload.get('strict_champion_n')}"
        )
    elif args.phase == "pool-sweep":
        imp = payload["imp_early_long_baseline"]
        top = (payload.get("top_by_excess_min_n") or [{}])[0]
        print(
            f"hook pool-tier sweep: {args.date_from} → {args.date_to} · {elapsed:.1f}s"
        )
        print(
            f"  imp_early_long n={imp.get('n')} excess={imp.get('mean_excess_pct')}%"
        )
        print(
            f"  eligible={payload.get('n_eligible')} / grid={payload.get('n_grid')}"
        )
        if top:
            print(
                f"  best: tier={top.get('pool_tier')} n={top.get('n')} "
                f"excess={top.get('mean_excess_pct')}%"
            )
        for row in payload.get("by_pool_tier") or []:
            print(
                f"  {row.get('tier')}: baseline_n={row.get('baseline_n')} "
                f"pos_min_n={row.get('rows_pos_excess_min_n')} "
                f"best_excess={row.get('best_excess')}%"
            )
    elif args.phase == "combo":
        hook = payload["hook_events"]["summary"]
        part = payload["portfolios"]["partitioned_2plus1"]
        base = payload["portfolios"]["c18acc_3slot_baseline"]
        print(
            f"hook_buy tactical slot: {args.date_from} → {args.date_to} · {elapsed:.1f}s"
        )
        print(
            f"  hook n={hook.get('n')} retrace={args.retrace_eps} "
            f"excess={hook.get('mean_excess_pct')}% hold={hook.get('mean_hold_polls')} polls"
        )
        print(
            f"  C18acc 3槽 CAGR={base.get('cagr_pct')}% MaxDD={base.get('max_drawdown_pct')}%"
        )
        print(
            f"  Partitioned 2+1 CAGR={part.get('cagr_pct')}% "
            f"MaxDD={part.get('max_drawdown_pct')}% ρ={part.get('daily_return_corr')}"
        )

    print(f"Wrote {out}")
    print(f"Wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
