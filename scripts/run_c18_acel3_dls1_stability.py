#!/usr/bin/env python3
"""rrg-mono-swap-accel（C18acc）vs C18-dls1 · 分年 / 子區間穩定性對照。"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_score_swap_c import (
    CHAMPION_SCORE_SWAP_C_VARIANT_ID,
    RRG_MONO_SWAP_ACCEL_SHORT,
    RRG_MONO_SWAP_ACCEL_SLUG,
    ScoreSwapCConfig,
    simulate_score_swap_c,
)
from report_paths import RESEARCH_RRG
from stock_db import DEFAULT_DB_PATH, connect

CHAMPION_ID = CHAMPION_SCORE_SWAP_C_VARIANT_ID
STABLE_ID = "C18-dls1"

VARIANTS: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig(
        STABLE_ID,
        "4日位移 down_left · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="down_left",
    ),
    ScoreSwapCConfig(
        CHAMPION_ID,
        "四日加速对称 · 卖转弱 · 买转强 · margin=0.05",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.05,
        accel_sell_negative_only=True,
        buy_sort_key="avg_accel_decel",
    ),
]

WINDOWS: list[tuple[str, str, str]] = [
    ("full", "2024-01-01", "2026-06-22"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026-YTD", "2026-01-01", "2026-06-22"),
    ("H1-24~25H1", "2024-01-01", "2025-06-30"),
    ("H2-25H2~26", "2025-07-01", "2026-06-22"),
]


def _load_context(conn, date_start: str, date_end: str):
    from market_benchmark import load_benchmark_close
    from market_breadth_ma import build_breadth_panel
    from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
    from rrg_mono_daily_brief import LENGTH
    from rrg_rotation import compute_rrg_panel
    from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
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


def _summarize_legs(periods: list[dict[str, Any]]) -> dict[str, Any]:
    s = summarize_periods(periods)
    n = len(periods)
    if n:
        s["mean_excess_pct"] = round(sum(p["excess_pct"] for p in periods) / n, 4)
        s["total_excess_pct"] = round(sum(p["excess_pct"] for p in periods), 4)
        s["mean_hold_days"] = round(sum(p["hold_days"] for p in periods) / n, 2)
    else:
        s["mean_excess_pct"] = None
        s["total_excess_pct"] = None
        s["mean_hold_days"] = None
    s["n_periods"] = n
    return s


def _by_entry_year(periods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in periods:
        buckets[str(p["entry_date"])[:4]].append(p)
    rows = []
    for year in sorted(buckets):
        s = _summarize_legs(buckets[year])
        rows.append({"year": year, **s})
    return rows


def run_stability(conn, *, date_start: str, date_end: str) -> dict[str, Any]:
    ctx = _load_context(conn, date_start, date_end)
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    variant_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []

    for cfg in VARIANTS:
        periods, summary = simulate_score_swap_c(
            conn,
            trade_dates=ctx["trade_dates"],
            full_dates=ctx["full_dates"],
            close=ctx["close"],
            bench=ctx["bench"],
            fresh_by_date=ctx["fresh_by_date"],
            zone_by_date=ctx["zone_by_date"],
            config=cfg,
            mono_by_date=ctx["mono_by_date"],
            kbar_cache=kbar_cache,
            rs_mom=ctx["rs_mom"],
            rs_ratio=ctx["rs_ratio"],
        )
        variant_rows.append(
            {
                "variant_id": cfg.variant_id,
                "full_window": summary,
                "by_entry_year": _by_entry_year(periods),
                "periods_count": len(periods),
            }
        )

    for label, w_start, w_end in WINDOWS:
        if label == "full":
            continue
        wctx = _load_context(conn, w_start, w_end)
        wcache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
        row: dict[str, Any] = {"window": label, "date_start": w_start, "date_end": w_end, "variants": {}}
        for cfg in VARIANTS:
            _, summary = simulate_score_swap_c(
                conn,
                trade_dates=wctx["trade_dates"],
                full_dates=wctx["full_dates"],
                close=wctx["close"],
                bench=wctx["bench"],
                fresh_by_date=wctx["fresh_by_date"],
                zone_by_date=wctx["zone_by_date"],
                config=cfg,
                mono_by_date=wctx["mono_by_date"],
                kbar_cache=wcache,
                rs_mom=wctx["rs_mom"],
                rs_ratio=wctx["rs_ratio"],
            )
            row["variants"][cfg.variant_id] = {
                "mean_excess_pct": summary.get("mean_excess_pct"),
                "n_periods": summary.get("n_periods"),
                "swaps_total": summary.get("swaps_total"),
                "win_rate_vs_bench_pct": summary.get("win_rate_vs_bench_pct"),
            }
        dls1 = row["variants"][STABLE_ID]["mean_excess_pct"] or 0.0
        champ = row["variants"][CHAMPION_ID]["mean_excess_pct"] or 0.0
        row["delta_champion_minus_dls1_pp"] = round(champ - dls1, 4)
        window_rows.append(row)

    full_dls1 = next(v for v in variant_rows if v["variant_id"] == STABLE_ID)
    full_champ = next(v for v in variant_rows if v["variant_id"] == CHAMPION_ID)
    fd = full_dls1["full_window"].get("mean_excess_pct") or 0.0
    fc = full_champ["full_window"].get("mean_excess_pct") or 0.0

    return {
        "date_start": date_start,
        "date_end": date_end,
        "champion_slug": RRG_MONO_SWAP_ACCEL_SLUG,
        "champion_short_name": RRG_MONO_SWAP_ACCEL_SHORT,
        "champion_id": CHAMPION_ID,
        "stable_id": STABLE_ID,
        "variants": variant_rows,
        "window_reruns": window_rows,
        "full_sample_delta_champion_minus_dls1_pp": round(fc - fd, 4),
    }


def _print_report(payload: dict[str, Any]) -> None:
    champ = payload.get("champion_id") or CHAMPION_ID
    print("\n=== 全樣本 · 依進場年（同一輪回測拆桶） ===")
    years = {y["year"] for v in payload["variants"] for y in v["by_entry_year"]}
    hdr = f"{'year':<6}" + "".join(f"{v['variant_id']:>18}" for v in payload["variants"]) + f"{'Δ(C18acc-dls1)':>16}"
    print(hdr)
    for year in sorted(years):
        cells = []
        for v in payload["variants"]:
            row = next((y for y in v["by_entry_year"] if y["year"] == year), None)
            if row and row.get("mean_excess_pct") is not None:
                cells.append(f"{row['mean_excess_pct']:>8.2f}% n={row['n_periods']:<3}")
            else:
                cells.append(f"{'—':>18}")
        dls = next(
            (y["mean_excess_pct"] for v in payload["variants"] if v["variant_id"] == STABLE_ID
             for y in v["by_entry_year"] if y["year"] == year),
            None,
        )
        c = next(
            (y["mean_excess_pct"] for v in payload["variants"] if v["variant_id"] == champ
             for y in v["by_entry_year"] if y["year"] == year),
            None,
        )
        delta = f"{c - dls:+.2f}pp" if dls is not None and c is not None else "—"
        print(f"{year:<6}" + "".join(cells) + f"{delta:>14}")

    print("\n=== 子區間獨立重跑 ===")
    print(f"{'window':<14} {'dls1 excess':>12} {'swaps':>6} {'C18acc':>12} {'swaps':>6} {'Δ pp':>8}")
    for w in payload["window_reruns"]:
        d = w["variants"][STABLE_ID]
        a = w["variants"][champ]
        print(
            f"{w['window']:<14} "
            f"{d.get('mean_excess_pct') or '—':>10}% {d.get('swaps_total') or 0:>6} "
            f"{a.get('mean_excess_pct') or '—':>10}% {a.get('swaps_total') or 0:>6} "
            f"{w.get('delta_champion_minus_dls1_pp'):>+8.2f}"
        )

    wins = sum(1 for w in payload["window_reruns"] if (w.get("delta_champion_minus_dls1_pp") or 0) > 0)
    print(f"\n子區間 C18acc 勝 dls1：{wins}/{len(payload['window_reruns'])}")
    print(f"全樣本 Δ(C18acc − dls1) = {payload['full_sample_delta_champion_minus_dls1_pp']:+.4f} pp")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rrg-mono-swap-accel (C18acc) vs dls1 stability")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_stability(conn, date_start=args.date_start, date_end=args.date_end)
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_c18_acc4_dls1_stability.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")
    _print_report(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
