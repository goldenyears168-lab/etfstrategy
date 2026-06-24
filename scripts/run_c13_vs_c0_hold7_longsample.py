#!/usr/bin/env python3
"""C13 vs C0 hold7 · 長樣本對照（可分段）。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, simulate_leg_c_variant
from research.backtest.rrg_mono_score_swap_c import ScoreSwapCConfig, simulate_score_swap_c
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels
from rrg_rotation import compute_rrg_panel
from stock_db import connect, DEFAULT_DB_PATH


def _run_window(conn, *, date_start: str, date_end: str, label: str) -> dict:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=20)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)

    from market_breadth_ma import build_breadth_panel

    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    _, c0_sum = simulate_leg_c_variant(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        zone_by_date=zone_by_date,
        fresh_by_date=fresh_by_date,
        config=c0_cfg,
    )

    c13_cfg = ScoreSwapCConfig(
        variant_id="C13",
        label="fresh · C0 · min_hold=5 · max_hold=10 · 5m 換",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
    )
    _, c13_sum = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=c13_cfg,
    )

    c0_ex = c0_sum.get("mean_excess_pct")
    c13_ex = c13_sum.get("mean_excess_pct")
    delta = round(float(c13_ex) - float(c0_ex), 4) if c0_ex is not None and c13_ex is not None else None
    return {
        "label": label,
        "date_start": date_start,
        "date_end": date_end,
        "c0_hold7": {
            "n_periods": c0_sum.get("n_periods"),
            "mean_excess_pct": c0_ex,
            "mean_hold_days": 7.0,
            "kbar_coverage_pct": c0_sum.get("kbar_coverage_pct"),
        },
        "c13": {
            "n_periods": c13_sum.get("n_periods"),
            "mean_excess_pct": c13_ex,
            "mean_hold_days": c13_sum.get("mean_hold_days"),
            "swaps_total": c13_sum.get("swaps_total"),
            "max_hold_exits": c13_sum.get("max_hold_exits"),
        },
        "delta_c13_minus_c0_pp": delta,
        "c13_wins": delta is not None and delta > 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C13 vs C0 hold7 long sample")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2025-12-31")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        windows = [
            _run_window(conn, date_start=args.date_start, date_end=args.date_end, label="full"),
            _run_window(conn, date_start="2024-01-01", date_end="2024-12-31", label="2024"),
            _run_window(conn, date_start="2025-01-01", date_end="2025-12-31", label="2025"),
        ]
    finally:
        conn.close()

    full = windows[0]
    payload = {
        "hypothesis": "C13（C0 進 · min_hold=5 · max_hold=10 · 5m 換）穩定優於 C0 hold7",
        "windows": windows,
        "verdict": {
            "full_sample_c13_wins": full["c13_wins"],
            "delta_pp": full["delta_c13_minus_c0_pp"],
            "stable_both_years": all(w["c13_wins"] for w in windows[1:]),
        },
    }

    out = args.out or ROOT / "reports/research/rrg" / f"c13_vs_c0_hold7_{args.date_start}_{args.date_end}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")
    for w in windows:
        print(
            f"{w['label']:5s} · C0={w['c0_hold7']['mean_excess_pct']}% (n={w['c0_hold7']['n_periods']}) "
            f"C13={w['c13']['mean_excess_pct']}% (n={w['c13']['n_periods']} swaps={w['c13']['swaps_total']}) "
            f"Δ={w['delta_c13_minus_c0_pp']}pp"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
