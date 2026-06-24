#!/usr/bin/env python3
"""C18acc · 盤中空槽進場模式 sweep · rank confirm vs 1m 確認 K 線。

SSG（fresh mono · seg_last 池）不變；僅 `entry_c_config`（C 腿 fill）對照：
  - C0 · confirm=1 · poll_px（冠軍 baseline）
  - C3 · confirm=2 · poll_px
  - C18acc-vwap / bone · confirm=1 · 1m 專家觸發
  - C18acc-cfm2-vwap · confirm=2 · VWAP reclaim
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import C18ACC_ENTRY_FILL_SWEEP, CVariantConfig
from research.backtest.rrg_mono_score_swap_c import (
    CHAMPION_SCORE_SWAP_C_VARIANT_ID,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from report_paths import RESEARCH_RRG
from rrg_rotation import compute_rrg_panel
from stock_db import DEFAULT_DB_PATH, connect

DATE_START = "2024-01-01"
DATE_END = "2026-06-22"


def _load_context(conn, date_start: str, date_end: str) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=20)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    return {
        "close": close,
        "bench": bench,
        "rs_ratio": rs_ratio,
        "rs_mom": rs_mom,
        "full_dates": full_dates,
        "trade_dates": trade_dates,
        "fresh_by_date": fresh_by_date,
        "mono_by_date": mono_by_date,
        "zone_by_date": zone_by_date,
    }


def _run_entry_variant(
    conn,
    ctx: dict[str, Any],
    entry_c_config: CVariantConfig,
    kbar_cache: dict,
) -> dict[str, Any]:
    champion = champion_score_swap_c_config()
    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=ctx["trade_dates"],
        full_dates=ctx["full_dates"],
        close=ctx["close"],
        bench=ctx["bench"],
        fresh_by_date=ctx["fresh_by_date"],
        zone_by_date=ctx["zone_by_date"],
        config=champion,
        mono_by_date=ctx["mono_by_date"],
        kbar_cache=kbar_cache,
        rs_mom=ctx["rs_mom"],
        rs_ratio=ctx["rs_ratio"],
        entry_c_config=entry_c_config,
    )
    n = len(periods)
    mean_ex = round(sum(p["excess_pct"] for p in periods) / n, 4) if n else None
    return {
        "variant_id": entry_c_config.variant_id,
        "label": entry_c_config.label,
        "confirm_bars": entry_c_config.confirm_bars,
        "entry_fill_mode": entry_c_config.entry_fill_mode,
        "n_periods": summary.get("n_periods", n),
        "swaps_total": summary.get("swaps_total"),
        "mean_excess_pct": mean_ex,
        "win_rate_vs_bench_pct": summary.get("win_rate_vs_bench_pct"),
        "mean_return_pct": summary.get("mean_return_pct"),
    }


def run_entry_sweep(
    conn,
    *,
    date_start: str = DATE_START,
    date_end: str = DATE_END,
    configs: list[CVariantConfig] | None = None,
) -> dict[str, Any]:
    ctx = _load_context(conn, date_start, date_end)
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    grid = configs or C18ACC_ENTRY_FILL_SWEEP
    variants: list[dict[str, Any]] = []
    for ecfg in grid:
        variants.append(_run_entry_variant(conn, ctx, ecfg, kbar_cache))
    baseline_ex = next(
        (v["mean_excess_pct"] for v in variants if v["variant_id"] == "C0"),
        variants[0]["mean_excess_pct"] if variants else None,
    )
    for v in variants:
        ex = v.get("mean_excess_pct")
        v["delta_vs_c0_pp"] = round(ex - baseline_ex, 4) if ex is not None and baseline_ex is not None else None
    return {
        "date_start": date_start,
        "date_end": date_end,
        "champion_strategy": CHAMPION_SCORE_SWAP_C_VARIANT_ID,
        "baseline_entry": "C0 · confirm=1 · poll_px",
        "baseline_mean_excess_pct": baseline_ex,
        "variants": variants,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc entry fill mode sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default=DATE_START)
    parser.add_argument("--date-end", default=DATE_END)
    parser.add_argument("--out-json", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_entry_sweep(conn, date_start=args.date_start, date_end=args.date_end)
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out_json or RESEARCH_RRG / f"{stamp}_c18acc_entry_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Baseline C0: {payload['baseline_mean_excess_pct']}%")
    for v in payload["variants"]:
        d = v.get("delta_vs_c0_pp")
        d_s = f"{d:+.4f}" if d is not None else "n/a"
        print(
            f"  {v['variant_id']:<18} {v.get('mean_excess_pct')}% "
            f"swaps={v.get('swaps_total')} Δ={d_s}pp · {v.get('entry_fill_mode')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
