#!/usr/bin/env python3
"""金額門檻 × RRG / 自適應門檻 Copytrade sweep.

  PYTHONPATH=src .venv/bin/python scripts/research/run_amount_rrg_copytrade_study.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_amount_rrg_copytrade_study.py --quick
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import REPORTS_RESEARCH  # noqa: E402
from research.backtest.amount_rrg_copytrade_study import (  # noqa: E402
    evaluate_amount_rrg_hypotheses,
    run_amount_rrg_sweep,
    select_amt_rrg_champions,
    write_artifacts,
)
from research.backtest.amount_rrg_copytrade_funnel_study import (  # noqa: E402
    DEFAULT_OOS_CUT,
    run_three_slot_funnel_study,
    write_artifacts as write_funnel_artifacts,
)
from research.backtest.amount_rrg_2day_sell_funnel_study import (  # noqa: E402
    run_2day_sell_funnel_study,
    write_artifacts as write_2day_artifacts,
)
from research.backtest.amount_rrg_cross_confirm_funnel_study import (  # noqa: E402
    run_cross_confirm_funnel_study,
    write_artifacts as write_cross_artifacts,
)
from research.backtest.amount_rrg_skilled_hand_rank_study import (  # noqa: E402
    run_skilled_hand_rank_study,
    write_artifacts as write_hand_rank_artifacts,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

OUT_DIR = REPORTS_RESEARCH / "dual-branch-copytrade"


def main() -> int:
    ap = argparse.ArgumentParser(description="Amount × RRG copytrade study")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument(
        "--quick",
        action="store_true",
        help="Smoke: xd only, 100M, gates none/leading/lead_improve, H9/12, 1 slot",
    )
    ap.add_argument(
        "--funnel-study",
        action="store_true",
        help="3 slots + Leading Dip orthogonal funnel hypotheses",
    )
    ap.add_argument(
        "--2day-sell-funnel",
        action="store_true",
        dest="two_day_sell_funnel",
        help="1-slot champion + consecutive-day / sell-side funnels (compound SSOT)",
    )
    ap.add_argument(
        "--cross-confirm-funnel",
        action="store_true",
        help="1-slot champion + Songjiang/KGI/institutional confirm funnels",
    )
    ap.add_argument(
        "--skilled-hand-rank",
        action="store_true",
        help="Soft re-rank Top1 by skilled-hand fingerprints (relationship/dominance)",
    )
    ap.add_argument("--oos-cut", default=DEFAULT_OOS_CUT)
    ap.add_argument(
        "--force-amount-m",
        type=float,
        default=None,
        help="Funnel sensitivity anchor in million NTD (50/65/80/100)",
    )
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    conn = connect(args.db)
    try:
        if args.skilled_hand_rank:
            payload = run_skilled_hand_rank_study(
                conn,
                window_start=args.start,
                window_end=args.end,
                oos_cut=args.oos_cut,
            )
            surv = payload["gate_surviving"]
            fair = payload["fair_compound_vs_c18acc"]
            print(
                f"survivor={surv['rank_id']} "
                f"full_compound={surv['full'].get('compound_slot_pct')} "
                f"oos_compound={surv['oos'].get('compound_slot_pct')} "
                f"switch%={surv['rank_meta'].get('switch_pct')}",
                flush=True,
            )
            c18 = fair.get("c18acc") or {}
            ov = fair.get("copytrade_baseline_overlap") or {}
            print(
                f"fair compound: C18acc={c18.get('compound_slot_pct')} "
                f"copy_overlap={ov.get('compound_slot_pct')}",
                flush=True,
            )
            for hid, hypothesis in payload["hypotheses"].items():
                print(f"  {hid}: {hypothesis.get('status')}", flush=True)
            if not args.no_write:
                paths = write_hand_rank_artifacts(payload, out_dir=OUT_DIR)
                for p in paths.values():
                    print(f"wrote {p}", flush=True)
            return 0

        if args.cross_confirm_funnel:
            payload = run_cross_confirm_funnel_study(
                conn,
                window_start=args.start,
                window_end=args.end,
                oos_cut=args.oos_cut,
            )
            surv = payload["gate_surviving"]
            fair = payload["fair_compound_vs_c18acc"]
            print(
                f"survivor={surv['funnel_id']} "
                f"full_compound={surv['full'].get('compound_slot_pct')} "
                f"oos_compound={surv['oos'].get('compound_slot_pct')}",
                flush=True,
            )
            c18 = (fair.get("c18acc") or {})
            ov = fair.get("copytrade_baseline_overlap") or {}
            print(
                f"fair compound: C18acc={c18.get('compound_slot_pct')} "
                f"copy_overlap={ov.get('compound_slot_pct')}",
                flush=True,
            )
            for hid, hypothesis in payload["hypotheses"].items():
                print(f"  {hid}: {hypothesis.get('status')}", flush=True)
            if not args.no_write:
                paths = write_cross_artifacts(payload, out_dir=OUT_DIR)
                for p in paths.values():
                    print(f"wrote {p}", flush=True)
            return 0

        if args.two_day_sell_funnel:
            payload = run_2day_sell_funnel_study(
                conn,
                window_start=args.start,
                window_end=args.end,
                oos_cut=args.oos_cut,
            )
            surv = payload["gate_surviving"]
            fair = payload["fair_compound_vs_c18acc"]
            print(
                f"survivor={surv['funnel_id']} "
                f"full_compound={surv['full'].get('compound_slot_pct')} "
                f"oos_compound={surv['oos'].get('compound_slot_pct')}",
                flush=True,
            )
            c18 = fair.get("c18acc") or {}
            ov = fair.get("copytrade_baseline_overlap") or {}
            print(
                f"fair compound: C18acc={c18.get('compound_slot_pct')} "
                f"copy_overlap={ov.get('compound_slot_pct')}",
                flush=True,
            )
            for hid, hypothesis in payload["hypotheses"].items():
                print(f"  {hid}: {hypothesis.get('status')}", flush=True)
            if not args.no_write:
                paths = write_2day_artifacts(payload, out_dir=OUT_DIR)
                for p in paths.values():
                    print(f"wrote {p}", flush=True)
            return 0

        if args.funnel_study:
            payload = run_three_slot_funnel_study(
                conn,
                window_start=args.start,
                window_end=args.end,
                oos_cut=args.oos_cut,
                force_amount_ntd=(
                    args.force_amount_m * 1_000_000.0
                    if args.force_amount_m is not None
                    else None
                ),
            )
            print(
                f"selected amount={payload['selected_amount_ntd']/1e6:.0f}M",
                flush=True,
            )
            for hid, hypothesis in payload["hypotheses"].items():
                print(f"  {hid}: {hypothesis.get('status')}", flush=True)
            if not args.no_write:
                stamp = (
                    f"{date.today():%Y%m%d}_{args.force_amount_m:g}m"
                    if args.force_amount_m is not None
                    else None
                )
                paths = write_funnel_artifacts(
                    payload, out_dir=OUT_DIR, stamp=stamp
                )
                print(f"wrote {paths['json']}", flush=True)
                print(f"wrote {paths['md']}", flush=True)
            return 0
        if args.quick:
            cells = run_amount_rrg_sweep(
                conn,
                window_start=args.start,
                window_end=args.end,
                modes=("xd",),
                fixed_amounts=(100_000_000.0,),
                rrg_gates=("none", "leading", "lead_improve"),
                include_adaptive=False,
                top_ns=(1,),
                entry_lags=(1,),
                holds=(9, 12),
                slot_counts=(1,),
                progress_every=10,
            )
        else:
            cells = run_amount_rrg_sweep(
                conn,
                window_start=args.start,
                window_end=args.end,
            )
        champions = select_amt_rrg_champions(cells)
        hypotheses = evaluate_amount_rrg_hypotheses(cells)
        champ = champions.get("by_period_return")
        if champ:
            print(
                f"champion: {champ.mode} thr={champ.thr_kind}/"
                f"{champ.min_threshold/1e6:.0f}M gate={champ.rrg_gate} "
                f"L{champ.entry_lag_days+1}H{champ.hold_trading_days} "
                f"slots={champ.n_slots} ret%={champ.period_return_pct} "
                f"α/日={champ.alpha_per_locked_day}",
                flush=True,
            )
        for hid in (
            "H_RRG_IMPROVES_APLD",
            "H_RRG_IMPROVES_RET",
            "H_ADAPT_P90",
        ):
            h = hypotheses.get(hid) or {}
            print(f"  {hid}: {h.get('status')}", flush=True)
        rec = hypotheses.get("recommended_sleeve") or {}
        if rec:
            print(
                f"  recommend: {rec.get('mode')} ≥{rec.get('threshold_label')} "
                f"gate={rec.get('rrg_gate')} {rec.get('strategy_id')} "
                f"ret%={rec.get('period_return_pct')}",
                flush=True,
            )
        if not args.no_write:
            paths = write_artifacts(
                cells=cells,
                champions=champions,
                hypotheses=hypotheses,
                window_start=args.start,
                window_end=args.end,
                out_dir=OUT_DIR,
            )
            print(f"wrote {paths['json']}", flush=True)
            print(f"wrote {paths['md']}", flush=True)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
