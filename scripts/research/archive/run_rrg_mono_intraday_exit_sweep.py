#!/usr/bin/env python3
"""RRG mono hold7 · 出場訊號 + 盤中賣點 sweep。

假說（相對 E0 hold7 收盤基線）：
  E1/E2 · RRG 象限轉弱（weakening/lagging）連續 N 日
  E3/E4 · 位移連續 3 日左下（ll_streak）· 收盤 vs 5m scale 盤中
  E5   · mono 加速中斷（mono_break）
  E6/E7 · D4 未再加速（accel_d4）· 收盤 vs 5m confirm
  E8   · seg_last 衰減至進場 85%
  E9   · 象限 lagging · 5m full_rrg 盤中
  E10  · 象限轉弱 · 15m scale confirm=2 · max_hold=10

用法：
  PYTHONPATH=src python scripts/run_rrg_mono_intraday_exit_sweep.py
  PYTHONPATH=src python scripts/run_rrg_mono_intraday_exit_sweep.py \\
    --date-start 2026-01-01 --date-end 2026-06-22
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_intraday_exit import (  # noqa: E402
    apply_exit_variant_to_periods,
    audit_kbar_fair_subset,
    run_exit_variant_sweep,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _render_md(payload: dict) -> str:
    lines = [
        "# RRG mono hold7 · 出場訊號 sweep",
        "",
        f"區間：{payload['date_start']} .. {payload['date_end']}",
        f"基線：{payload['baseline_variant_id']}",
        "",
        payload.get("ssg_note", ""),
        "",
        "## 假說對照",
        "",
    ]
    for k, v in (payload.get("hypotheses") or {}).items():
        lines.append(f"- **{k}**：{v}")
    lines += [
        "",
        "| rank | id | 訊號 | 盤中 | streak | min | max | n | 均超額% | 均持有日 | kbar% | vs基線 |",
        "|------|----|------|------|--------|-----|-----|---|---------|----------|-------|--------|",
    ]
    ranked = sorted(
        payload["summaries"],
        key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("n_periods") or 0)),
    )
    for i, s in enumerate(ranked, start=1):
        lines.append(
            f"| {i} | {s.get('variant_id')} | {s.get('signal_mode')} "
            f"| {s.get('timing_mode')} | {s.get('streak_days')} "
            f"| {s.get('min_hold_days')} | {s.get('max_hold_days')} "
            f"| {s.get('n_periods')} | {s.get('mean_excess_pct')} "
            f"| {s.get('mean_hold_days')} | {s.get('kbar_coverage_pct')} "
            f"| {s.get('delta_vs_baseline_pp')} |"
        )
    best = payload.get("best")
    if best:
        lines += [
            "",
            "## 冠軍",
            "",
            f"- **{best.get('variant_id')}** · {best.get('label')}",
            f"- 均超額 {best.get('mean_excess_pct')}% · 均持有 {best.get('mean_hold_days')} 日",
            f"- vs 基線 {best.get('delta_vs_baseline_pp')} pp",
            "",
            "```json",
            json.dumps(best, ensure_ascii=False, indent=2),
            "```",
        ]
    ref = payload.get("reference_entry") or {}
    lines += [
        "",
        "## 進場參照（A 腿）",
        "",
        f"n={ref.get('n_periods')} · 均超額 {ref.get('mean_excess_pct')}%",
        "",
        "---",
        "模組：`scripts/run_rrg_mono_intraday_exit_sweep.py`",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG mono hold7 exit-signal sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--baseline", default="E0")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    parser.add_argument(
        "--kbar-fair-only",
        action="store_true",
        help="另輸出 kbar 100%% 覆蓋子樣本摘要（需先 backfill stock_kbar_1m）",
    )
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_exit_variant_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            baseline_variant_id=args.baseline,
        )
        if args.kbar_fair_only:
            from market_benchmark import load_benchmark_close
            from research.backtest.finpilot_local_backtest import load_price_panels
            from research.backtest.rrg_mono_backtest import (
                build_fresh_mono_calendar,
                simulate_mono_hold7,
            )
            from market_breadth_ma import build_breadth_panel
            from research.backtest.rrg_mono_intraday_exit import (
                DEFAULT_EXIT_SWEEP,
            )
            from rrg_rotation import compute_rrg_panel
            from research.backtest.rrg_mono_intraday_ab import LENGTH

            close, _, _ = load_price_panels(conn)
            bench = load_benchmark_close(conn).reindex(close.index)
            rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
            full_dates = close.index.astype(str).tolist()
            trade_dates = [d for d in full_dates if args.date_start <= d <= args.date_end]
            fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
            panel = build_breadth_panel(
                conn, date_start=args.date_start, date_end=args.date_end
            )
            zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
            base_periods, _ = simulate_mono_hold7(
                conn,
                trade_dates=trade_dates,
                full_dates=full_dates,
                close=close,
                zone_by_date=zone_by_date,
                fresh_by_date=fresh_by_date,
            )
            fair_dates = set(audit_kbar_fair_subset(conn, base_periods))
            fair_base = [p for p in base_periods if str(p["exit_date"]) in fair_dates]
            fair_summaries = []
            kbar_cache: dict = {}
            for cfg in DEFAULT_EXIT_SWEEP:
                periods, summary = apply_exit_variant_to_periods(
                    conn,
                    base_periods=fair_base,
                    close=close,
                    bench=bench,
                    rs_ratio=rs_ratio,
                    rs_mom=rs_mom,
                    full_dates=full_dates,
                    config=cfg,
                    kbar_cache=kbar_cache,
                )
                summary["kbar_fair_dates"] = len(fair_dates)
                fair_summaries.append(summary)
            payload["kbar_fair_summaries"] = fair_summaries
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_rrg_mono_hold7_exit_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md_path = args.md or out.with_suffix(".md")
    md_path.write_text(_render_md(payload), encoding="utf-8")
    print(f"Wrote {md_path}")

    best = payload.get("best") or {}
    print(
        f"Best {best.get('variant_id')}: n={best.get('n_periods')} "
        f"mean_excess={best.get('mean_excess_pct')} "
        f"delta={best.get('delta_vs_baseline_pp')}pp"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
