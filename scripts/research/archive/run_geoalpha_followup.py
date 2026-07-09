#!/usr/bin/env python3
"""GeoAlpha Phase 2 follow-up · gate sweep · M2 OOS · C18acc 2+1 combo."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from project_config import DEFAULT_ETF_CODES, parse_etf_codes
from project_dotenv import load_project_dotenv
from render_rrg_universe_html import _load_trading_dates_range
from research.backtest.archive.dual_wma_tactical_slot_backtest import TacticalExitParams
from research.backtest.archive.geoalpha_ablation_backtest import (
    IS_DATE_FROM,
    IS_DATE_TO,
    OOS_DATE_FROM,
    OOS_DATE_TO,
    render_geoalpha_gate_sweep_md,
    render_geoalpha_m2_combo_md,
    render_geoalpha_m2_oos_holdout_md,
    run_geo_m2_tactical_slot_study,
    run_geoalpha_gate_sweep,
    run_m2_oos_holdout,
    write_geoalpha_followup_report,
)
from stock_db import DEFAULT_DB_PATH, connect


def main() -> int:
    load_project_dotenv()
    ap = argparse.ArgumentParser(description="GeoAlpha follow-up research")
    ap.add_argument(
        "--phase",
        choices=("gate-sweep", "oos", "combo", "all"),
        default="all",
        help="gate-sweep · oos · combo · all",
    )
    ap.add_argument("--date-from", default=IS_DATE_FROM)
    ap.add_argument("--date-to", default=IS_DATE_TO)
    ap.add_argument("--oos-from", default=OOS_DATE_FROM)
    ap.add_argument("--oos-to", default=OOS_DATE_TO)
    ap.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--capital", type=float, default=50_000.0)
    ap.add_argument("--exit-cap-days", type=int, default=3)
    args = ap.parse_args()

    etf_codes = parse_etf_codes(args.etf_codes)
    exit_params = TacticalExitParams(cap_days=args.exit_cap_days)
    conn = connect(args.db)
    reports: list[Path] = []

    try:
        t0 = time.perf_counter()

        if args.phase in ("gate-sweep", "all"):
            is_dates = _load_trading_dates_range(
                conn, date_from=args.date_from, date_to=args.date_to
            )
            if not is_dates:
                raise ValueError(f"無交易日 {args.date_from} → {args.date_to}")
            payload = run_geoalpha_gate_sweep(
                conn, is_dates, etf_codes, exit_params=exit_params
            )
            _, md = write_geoalpha_followup_report(
                payload,
                stem="geoalpha_ablation_gate_sweep",
                render_md=render_geoalpha_gate_sweep_md,
            )
            reports.append(md)
            print("Gate sweep:")
            for r in payload.get("baseline_rows") or []:
                print(f"  {r['model']}: n={r.get('n')} excess={r.get('mean_excess_pct')}%")
            for r in payload.get("gate_rows") or []:
                print(
                    f"  {r.get('struct_gate')} {r['model']}: n={r.get('n')} "
                    f"excess={r.get('mean_excess_pct')}%"
                )

        if args.phase in ("oos", "all"):
            is_dates = _load_trading_dates_range(
                conn, date_from=args.date_from, date_to=args.date_to
            )
            oos_dates = _load_trading_dates_range(
                conn, date_from=args.oos_from, date_to=args.oos_to
            )
            payload = run_m2_oos_holdout(
                conn,
                is_dates=is_dates,
                oos_dates=oos_dates,
                etf_codes=etf_codes,
                exit_params=exit_params,
            )
            _, md = write_geoalpha_followup_report(
                payload,
                stem="geoalpha_m2_oos_holdout",
                render_md=render_geoalpha_m2_oos_holdout_md,
            )
            reports.append(md)
            g2 = payload.get("g2_oos_holdout") or {}
            print(
                f"OOS holdout: IS n={payload['is'].get('n')} excess={payload['is'].get('mean_excess_pct')}% · "
                f"OOS n={payload['oos'].get('n')} excess={payload['oos'].get('mean_excess_pct')}% · "
                f"G2={'PASS' if g2.get('passed') else 'FAIL'}"
            )

        if args.phase in ("combo", "all"):
            is_dates = _load_trading_dates_range(
                conn, date_from=args.date_from, date_to=args.date_to
            )
            if not is_dates:
                raise ValueError(f"無交易日 {args.date_from} → {args.date_to}")
            payload = run_geo_m2_tactical_slot_study(
                conn,
                dates=is_dates,
                etf_codes=etf_codes,
                total_capital=args.capital,
                exit_params=exit_params,
            )
            _, md = write_geoalpha_followup_report(
                payload,
                stem="geoalpha_c18acc_2plus1",
                render_md=render_geoalpha_m2_combo_md,
            )
            reports.append(md)
            pf = payload["portfolios"]
            print(
                f"Combo: baseline CAGR={pf['c18acc_3slot_baseline'].get('cagr_pct')}% · "
                f"2+1 CAGR={pf['partitioned_2plus1'].get('cagr_pct')}% · "
                f"M2 excess={payload['tactical_events']['summary'].get('mean_excess_pct')}%"
            )

        elapsed = time.perf_counter() - t0
        print(f"Done in {elapsed:.1f}s")
        for p in reports:
            print(f"Wrote {p}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
