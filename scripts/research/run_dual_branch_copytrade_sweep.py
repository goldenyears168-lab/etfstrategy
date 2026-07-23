#!/usr/bin/env python3
"""雙分點 Copytrade · metric×mode×門檻×L×H×槽 sweep.

  PYTHONPATH=src .venv/bin/python scripts/research/run_dual_branch_copytrade_sweep.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_dual_branch_copytrade_sweep.py \\
      --start 2024-07-01 --quick
  PYTHONPATH=src .venv/bin/python scripts/research/run_dual_branch_copytrade_sweep.py \\
      --amount-focus
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from copytrade.dual_branch_signals import (  # noqa: E402
    AMOUNT_FOCUS_MIN_NET_AMOUNTS,
    AMOUNT_FOCUS_MODES,
    threshold_label,
)
from report_paths import REPORTS_RESEARCH  # noqa: E402
from research.backtest.dual_branch_copytrade_sweep import (  # noqa: E402
    AMOUNT_FOCUS_ENTRY_LAGS,
    AMOUNT_FOCUS_FILE_STEM,
    DEFAULT_HOLDS,
    DEFAULT_MIN_NET_AMOUNTS,
    DEFAULT_MIN_NET_SHARES,
    DEFAULT_SLOTS,
    DEFAULT_TOP_NS,
    evaluate_amount_focus,
    evaluate_hypotheses,
    run_dual_phase_a_sweep,
    select_champions,
    write_artifacts,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

OUT_DIR = REPORTS_RESEARCH / "dual-branch-copytrade"


def main() -> int:
    ap = argparse.ArgumentParser(description="Dual-branch copytrade parameter sweep")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument(
        "--quick",
        action="store_true",
        help="Smoke grid: lots 100k/500k, amount 10M/50M, top1, H 9/12, slots 1/9",
    )
    ap.add_argument(
        "--amount-focus",
        action="store_true",
        help=(
            "Amount-primary deep grid (10–150M) + lots baseline for density match; "
            "modes sj/xd/and/sum; L1–L3"
        ),
    )
    ap.add_argument(
        "--no-volume-filter",
        action="store_true",
        help="Disable 20d avg volume ≥ 200 張 filter",
    )
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    if args.amount_focus:
        modes = AMOUNT_FOCUS_MODES
        metrics = ("lots", "amount")
        min_shares = DEFAULT_MIN_NET_SHARES
        min_amts = AMOUNT_FOCUS_MIN_NET_AMOUNTS
        top_ns = DEFAULT_TOP_NS
        entry_lags = AMOUNT_FOCUS_ENTRY_LAGS
        holds = DEFAULT_HOLDS
        slots = DEFAULT_SLOTS
        file_stem = AMOUNT_FOCUS_FILE_STEM
        strategy_tag = "dual-branch-amount-focus"
        branch_label = "雙分點 · 金額門檻專項（松江 × 新店）"
    elif args.quick:
        modes = None  # default MODES inside sweep
        metrics = None
        min_shares = (100_000.0, 500_000.0)
        min_amts = (10_000_000.0, 50_000_000.0)
        top_ns = (1,)
        entry_lags = None
        holds = (9, 12)
        slots = (1, 9)
        file_stem = "dual_branch_copytrade_sweep"
        strategy_tag = "dual-branch"
        branch_label = "雙分點（元大-松江 × 富邦-新店）"
    else:
        modes = None
        metrics = None
        min_shares = DEFAULT_MIN_NET_SHARES
        min_amts = DEFAULT_MIN_NET_AMOUNTS
        top_ns = DEFAULT_TOP_NS
        entry_lags = None
        holds = DEFAULT_HOLDS
        slots = DEFAULT_SLOTS
        file_stem = "dual_branch_copytrade_sweep"
        strategy_tag = "dual-branch"
        branch_label = "雙分點（元大-松江 × 富邦-新店）"

    min_vol = None if args.no_volume_filter else 200_000.0
    conn = connect(args.db)
    try:
        n_sj = conn.execute(
            """
            SELECT COUNT(*) FROM stock_broker_branch_daily
            WHERE securities_trader_id = '9801' AND source = 'finmind'
              AND trade_date >= ? AND trade_date <= ?
            """,
            (args.start, args.end),
        ).fetchone()[0]
        n_xd = conn.execute(
            """
            SELECT COUNT(*) FROM stock_broker_branch_daily
            WHERE securities_trader_id = '9661' AND source = 'finmind'
              AND trade_date >= ? AND trade_date <= ?
            """,
            (args.start, args.end),
        ).fetchone()[0]
        if int(n_sj) == 0 or int(n_xd) == 0:
            print(
                "ERROR: missing tape rows "
                f"(sj={n_sj} xd={n_xd}). Backfill both branches first.",
                file=sys.stderr,
            )
            return 2
        print(
            f"tape sj={n_sj} xd={n_xd} window={args.start}..{args.end} "
            f"quick={args.quick} amount_focus={args.amount_focus}",
            flush=True,
        )
        sweep_kwargs: dict = {
            "window_start": args.start,
            "window_end": args.end,
            "min_net_shares": min_shares,
            "min_net_amounts": min_amts,
            "top_ns": top_ns,
            "holds": holds,
            "slot_counts": slots,
            "min_avg_volume_shares": min_vol,
        }
        if modes is not None:
            sweep_kwargs["modes"] = modes
        if metrics is not None:
            sweep_kwargs["metrics"] = metrics
        if entry_lags is not None:
            sweep_kwargs["entry_lags"] = entry_lags

        cells = run_dual_phase_a_sweep(conn, **sweep_kwargs)
        champions = select_champions(cells)
        if args.amount_focus:
            hypotheses = evaluate_amount_focus(cells)
            # Champions from amount cells only (amount-primary study).
            amt_only = [c for c in cells if c.metric == "amount"]
            if amt_only:
                champions = select_champions(amt_only)
        else:
            hypotheses = evaluate_hypotheses(cells)

        champ = champions.get("by_period_return")
        if champ:
            print(
                "champion: "
                f"{champ.mode}/{champ.metric}≥"
                f"{threshold_label(champ.metric, champ.min_threshold)} "  # type: ignore[arg-type]
                f"L{champ.entry_lag_days + 1}H{champ.hold_trading_days} "
                f"top{champ.top_n} slots={champ.n_slots} "
                f"ret%={champ.period_return_pct} α={champ.recycled_total_alpha_ntd} "
                f"α/日={champ.alpha_per_locked_day}",
                flush=True,
            )
        else:
            print("champion: none", flush=True)

        if args.amount_focus:
            haf = hypotheses.get("H_AMOUNT_FOCUS") or {}
            print(f"  H_AMOUNT_FOCUS: {haf.get('status')}", flush=True)
            rec = hypotheses.get("recommended_amount_sleeve") or {}
            if rec:
                print(
                    "  recommend: "
                    f"{rec.get('mode')}/{rec.get('metric')}≥{rec.get('threshold_label')} "
                    f"{rec.get('strategy_id')} top{rec.get('top_n')} "
                    f"slots={rec.get('n_slots')} ret%={rec.get('period_return_pct')}",
                    flush=True,
                )
        else:
            for hid in (
                "H1_amount_beats_lots",
                "H2_and_or_sum_beats_single_apld",
                "H3_or_dilution",
            ):
                h = hypotheses.get(hid) or {}
                print(f"  {hid}: {h.get('status')}", flush=True)

        if not args.no_write:
            from research.backtest.dual_branch_copytrade_sweep import (
                DEFAULT_ENTRY_LAGS,
                MODES as _MODES,
                METRICS as _METRICS,
            )

            grid_meta = {
                "modes": list(modes) if modes is not None else list(_MODES),
                "metrics": list(metrics) if metrics is not None else list(_METRICS),
                "min_net_shares": list(min_shares),
                "min_net_amounts": list(min_amts),
                "top_ns": list(top_ns),
                "entry_lags": list(
                    entry_lags if entry_lags is not None else DEFAULT_ENTRY_LAGS
                ),
                "holds": list(holds),
                "slots": list(slots),
                "amount_focus": bool(args.amount_focus),
            }
            paths = write_artifacts(
                cells=cells,
                champions=champions,
                hypotheses=hypotheses,
                window_start=args.start,
                window_end=args.end,
                out_dir=OUT_DIR,
                file_stem=file_stem,
                strategy_tag=strategy_tag,
                branch_label=branch_label,
                grid_meta=grid_meta,
            )
            print(f"wrote {paths['json']}", flush=True)
            print(f"wrote {paths['md']}", flush=True)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
