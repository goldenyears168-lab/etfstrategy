#!/usr/bin/env python3
"""C18acc 空槽頻率分析 · 量化 EOD 槽位佔用分佈 + fresh pool 大小。

用法：
  python scripts/analyze_c18acc_slot_utilization.py [--date-start 2024-01-01] [--date-end 2026-06-23]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from market_benchmark import load_benchmark_close  # noqa: E402
from market_breadth_ma import build_breadth_panel  # noqa: E402
from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.finpilot_local_backtest import load_price_panels  # noqa: E402
from research.backtest.rrg_mono_backtest import (  # noqa: E402
    build_fresh_mono_calendar,
    build_mono_up_calendar,
    build_mono_up_fresh_calendar,
)
from research.backtest.rrg_mono_score_swap_c import (  # noqa: E402
    C18acc_POOL_MERGE_SWEEP,
    ScoreSwapCConfig,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar  # noqa: E402
from rrg_mono_daily_brief import LOOKBACK, MAX_SLOTS  # noqa: E402
from rrg_rotation import compute_rrg_panel  # noqa: E402
from rrg_mono_daily_brief import LENGTH  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _analyze_slots(
    slot_snapshots: dict[str, list[dict]],
    fresh_by_date: dict[str, list],
) -> dict:
    """Compute slot utilization stats from EOD snapshots."""
    trade_dates = sorted(slot_snapshots)
    n_days = len(trade_dates)
    if n_days == 0:
        return {}

    fill_counts: list[int] = []
    fresh_sizes: list[int] = []
    zero_fresh_days: list[str] = []

    for d in trade_dates:
        snap = slot_snapshots[d]
        fill_counts.append(len(snap))
        fs = len(fresh_by_date.get(d, []))
        fresh_sizes.append(fs)
        if fs == 0:
            zero_fresh_days.append(d)

    dist = Counter(fill_counts)
    mean_fill = sum(fill_counts) / n_days
    mean_empty = MAX_SLOTS - mean_fill
    pct_all_full = dist[MAX_SLOTS] / n_days * 100
    pct_any_empty = (n_days - dist[MAX_SLOTS]) / n_days * 100
    pct_0_slots = dist[0] / n_days * 100
    pct_1_slot = dist[1] / n_days * 100
    pct_2_slots = dist[2] / n_days * 100

    return {
        "n_trade_days": n_days,
        "max_slots": MAX_SLOTS,
        "mean_fill": round(mean_fill, 3),
        "mean_empty": round(mean_empty, 3),
        "pct_all_full_3_3": round(pct_all_full, 1),
        "pct_any_empty": round(pct_any_empty, 1),
        "pct_0_filled": round(pct_0_slots, 1),
        "pct_1_filled": round(pct_1_slot, 1),
        "pct_2_filled": round(pct_2_slots, 1),
        "fill_dist": {str(k): v for k, v in sorted(dist.items())},
        "mean_fresh_pool_size": round(sum(fresh_sizes) / n_days, 2),
        "pct_days_fresh_pool_zero": round(len(zero_fresh_days) / n_days * 100, 1),
        "n_days_fresh_pool_zero": len(zero_fresh_days),
        "zero_fresh_sample": zero_fresh_days[-10:],
    }


def run(
    conn,
    *,
    date_start: str,
    date_end: str,
) -> dict:
    from rrg_mono_daily_brief import LENGTH  # local
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    print(f"Trade dates: {trade_dates[0]} ~ {trade_dates[-1]} ({len(trade_dates)} days)", flush=True)

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates, lookback=4)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    mono_up_by_date = build_mono_up_calendar(conn, trade_dates, require_disp=True)
    mono_up_fresh_by_date = build_mono_up_fresh_calendar(conn, trade_dates)

    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    champ = champion_score_swap_c_config()

    # — Champion: fresh pool (baseline) —
    slot_snapshots: dict[str, list[dict]] = {}
    _, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=champ,
        mono_by_date=mono_by_date,
        mono_up_by_date=mono_up_by_date,
        mono_up_fresh_by_date=mono_up_fresh_by_date,
        rs_mom=rs_mom,
        rs_ratio=rs_ratio,
        slot_snapshots=slot_snapshots,
    )
    stats = _analyze_slots(slot_snapshots, fresh_by_date)
    print(
        f"\n── C18acc (fresh) ──"
        f"\n  mean_excess={summary['mean_excess_pct']}%  n={summary['n_periods']}  swaps={summary['swaps_total']}"
        f"\n  mean fill={stats['mean_fill']}/{stats['max_slots']}  mean_empty={stats['mean_empty']}"
        f"\n  3/3 full: {stats['pct_all_full_3_3']}%  any_empty: {stats['pct_any_empty']}%"
        f"\n  0 filled: {stats['pct_0_filled']}%  1 filled: {stats['pct_1_filled']}%  2 filled: {stats['pct_2_filled']}%"
        f"\n  fresh pool=0 on {stats['n_days_fresh_pool_zero']} days ({stats['pct_days_fresh_pool_zero']}%)"
        f"\n  mean fresh pool size: {stats['mean_fresh_pool_size']} stocks",
        flush=True,
    )

    # — Compare: fresh_union_accel pool (wider) —
    union_cfg = next(
        (c for c in C18acc_POOL_MERGE_SWEEP if c.variant_id == "C18acc-fresh-union-accel"),
        None,
    )
    union_stats = None
    union_summary = None
    if union_cfg is not None:
        slot_snapshots_u: dict[str, list[dict]] = {}
        # fresh_union_accel needs fresh_by_date with lb=4 (same)
        _, union_summary = simulate_score_swap_c(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            bench=bench,
            fresh_by_date=fresh_by_date,
            zone_by_date=zone_by_date,
            config=union_cfg,
            mono_by_date=mono_by_date,
            mono_up_by_date=mono_up_by_date,
            mono_up_fresh_by_date=mono_up_fresh_by_date,
            rs_mom=rs_mom,
            rs_ratio=rs_ratio,
            slot_snapshots=slot_snapshots_u,
        )
        # For union pool, "fresh_by_date" still drives fresh; but entries can also come from mono_tier2 w/ accel
        union_stats = _analyze_slots(slot_snapshots_u, fresh_by_date)
        print(
            f"\n── C18acc-fresh-union-accel ──"
            f"\n  mean_excess={union_summary['mean_excess_pct']}%  n={union_summary['n_periods']}  swaps={union_summary['swaps_total']}"
            f"\n  mean fill={union_stats['mean_fill']}/{union_stats['max_slots']}  mean_empty={union_stats['mean_empty']}"
            f"\n  3/3 full: {union_stats['pct_all_full_3_3']}%  any_empty: {union_stats['pct_any_empty']}%",
            flush=True,
        )

    return {
        "date_start": date_start,
        "date_end": date_end,
        "champion": {
            "variant_id": champ.variant_id,
            "summary": summary,
            "slot_utilization": stats,
        },
        "union_accel": (
            {
                "variant_id": union_cfg.variant_id if union_cfg else None,
                "summary": union_summary,
                "slot_utilization": union_stats,
            }
            if union_cfg
            else None
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-23")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        result = run(conn, date_start=args.date_start, date_end=args.date_end)
    finally:
        conn.close()

    from datetime import date as _date
    stamp = _date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_c18acc_slot_utilization.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
