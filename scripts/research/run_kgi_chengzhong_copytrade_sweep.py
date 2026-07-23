#!/usr/bin/env python3
"""凱基-城中 Copytrade · Phase A sweep + Phase B swap overlay.

Same grid / champions / artifacts as 元大-松江 / 富邦-新店.

  PYTHONPATH=src .venv/bin/python scripts/research/run_kgi_chengzhong_copytrade_sweep.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_kgi_chengzhong_copytrade_sweep.py \\
      --start 2024-07-01 --quick
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from copytrade.branch_signals import (  # noqa: E402
    DEFAULT_TRADER_ID_KGI_CHENGZHONG,
)
from report_paths import REPORTS_RESEARCH  # noqa: E402
from research.backtest.yuanta_songjiang_copytrade_sweep import (  # noqa: E402
    DEFAULT_HOLDS,
    DEFAULT_MIN_NETS,
    DEFAULT_SLOTS,
    DEFAULT_TOP_NS,
    run_phase_a_sweep,
    run_phase_b_swap,
    select_champions,
    write_artifacts,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

BRANCH_LABEL = "凱基-城中"
STRATEGY_TAG = "kgi-chengzhong"
ETF_CODE_PROXY = "KGI_CZ"
FILE_STEM = "kgi_chengzhong_copytrade_sweep"
OUT_DIR = REPORTS_RESEARCH / "kgi-chengzhong-copytrade"


def main() -> int:
    ap = argparse.ArgumentParser(description="凱基-城中 copytrade parameter sweep")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--trader-id", default=DEFAULT_TRADER_ID_KGI_CHENGZHONG)
    ap.add_argument(
        "--quick",
        action="store_true",
        help="Small grid for smoke: nets 100k/200k, top 1/3, H 5/9/15, slots 1/3/9",
    )
    ap.add_argument(
        "--no-volume-filter",
        action="store_true",
        help="Disable 20d avg volume ≥ 200 張 filter",
    )
    ap.add_argument(
        "--skip-phase-b",
        action="store_true",
        help="Only run Phase A",
    )
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    if args.quick:
        min_nets = (100_000.0, 200_000.0)
        top_ns = (1, 3)
        holds = (5, 9, 15)
        slots = (1, 3, 9)
    else:
        min_nets = DEFAULT_MIN_NETS
        top_ns = DEFAULT_TOP_NS
        holds = DEFAULT_HOLDS
        slots = DEFAULT_SLOTS

    min_vol = None if args.no_volume_filter else 200_000.0
    conn = connect(args.db)
    try:
        n_tape = conn.execute(
            """
            SELECT COUNT(*) FROM stock_broker_branch_daily
            WHERE securities_trader_id = ? AND source = 'finmind'
              AND trade_date >= ? AND trade_date <= ?
            """,
            (args.trader_id, args.start, args.end),
        ).fetchone()[0]
        if int(n_tape) == 0:
            print(
                "ERROR: no stock_broker_branch_daily rows for trader. "
                "Run scripts/research/backfill_kgi_chengzhong_tape.py first.",
                file=sys.stderr,
            )
            return 2
        print(
            f"tape_rows={n_tape} trader={args.trader_id} "
            f"window={args.start}..{args.end} quick={args.quick}",
            flush=True,
        )
        cells = run_phase_a_sweep(
            conn,
            trader_id=str(args.trader_id),
            window_start=args.start,
            window_end=args.end,
            min_nets=min_nets,
            top_ns=top_ns,
            holds=holds,
            slot_counts=slots,
            min_avg_volume_shares=min_vol,
            include_match_horizon=not args.quick,
            etf_code_proxy=ETF_CODE_PROXY,
            strategy_label_prefix="凱基城中",
        )
        champions = select_champions(cells)
        champ = champions.get("by_period_return")
        phase_b = None
        if not args.skip_phase_b and champ is not None:
            print(
                "phaseB swap on champion "
                f"L{champ.entry_lag_days + 1}H{champ.hold_trading_days} "
                f"slots={champ.n_slots} net={champ.min_net_shares:.0f} top={champ.top_n}",
                flush=True,
            )
            phase_b = run_phase_b_swap(
                conn,
                champ,
                trader_id=str(args.trader_id),
                window_start=args.start,
                window_end=args.end,
                min_avg_volume_shares=min_vol,
                etf_code_proxy=ETF_CODE_PROXY,
            )
            print(
                f"  swap_beats_skip={phase_b.get('swap_beats_skip')} "
                f"Δret={phase_b.get('delta', {}).get('period_return_pct')} "
                f"swaps={phase_b.get('delta', {}).get('n_swaps')}",
                flush=True,
            )
        if champ:
            print(
                "champion: "
                f"L{champ.entry_lag_days + 1}H{champ.hold_trading_days} "
                f"slots={champ.n_slots} net≥{champ.min_net_shares:.0f} top{champ.top_n} "
                f"ret%={champ.period_return_pct} α={champ.recycled_total_alpha_ntd} "
                f"capture%={champ.signal_capture_pct}",
                flush=True,
            )
        else:
            print("champion: none (no cells)", flush=True)
        if not args.no_write:
            paths = write_artifacts(
                cells=cells,
                champions=champions,
                phase_b=phase_b,
                window_start=args.start,
                window_end=args.end,
                trader_id=str(args.trader_id),
                out_dir=OUT_DIR,
                strategy_tag=STRATEGY_TAG,
                branch_label=BRANCH_LABEL,
                file_stem=FILE_STEM,
                etf_code_proxy=ETF_CODE_PROXY,
            )
            print(f"wrote {paths['json']}", flush=True)
            print(f"wrote {paths['md']}", flush=True)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
