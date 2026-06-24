#!/usr/bin/env python3
"""RRG mono hold7 · 建倉 A/B/C 對照。

A · 收盤 seg_last 填槽（現行 hold7）
B · 日線 fresh 前十 → 盤中定點（預設 10:00）seg_last 重排
C · 日線 fresh 前十 → 盤中每 N 分鐘動態重排（預設 5 分）

用法：
  PYTHONPATH=src python scripts/run_rrg_mono_intraday_ab.py
  PYTHONPATH=src python scripts/run_rrg_mono_intraday_ab.py \\
    --date-start 2026-05-24 --date-end 2026-06-22 \\
    --intraday-minute 10:00 --rebalance-interval-min 5
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_intraday_ab import (  # noqa: E402
    LEG_LABELS,
    run_hold7_ab_comparison,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _render_md(payload: dict) -> str:
    lines = [
        "# RRG mono hold7 · 建倉 A/B/C 對照",
        "",
        f"區間：{payload['date_start']} .. {payload['date_end']}",
        f"B 盤中定點：{payload['intraday_minute']} · C 輪詢間隔：{payload['rebalance_interval_min']} 分",
        "",
        "| 腿 | 說明 | n | 均超額% | 勝率% | kbar% | vs A (pp) |",
        "|----|------|---|---------|-------|-------|-----------|",
    ]
    for leg_id in ("A", "B", "C"):
        item = payload["legs"][leg_id]
        s = item["summary"]
        lines.append(
            f"| {leg_id} | {item['label']} | {item['n_periods']} "
            f"| {s.get('mean_excess_pct')} | {s.get('win_rate_vs_bench_pct')} "
            f"| {s.get('kbar_coverage_pct')} | {item.get('delta_vs_a_pp')} |"
        )
    lines += [
        "",
        "## 解讀",
        "",
        "- **A** = 現行 hold7（收盤 seg_last 排序 · D4 close 進場）",
        "- **B** = 訊號仍 D4 收盤 fresh · shortlist 前十 · 單一盤中時點重排後填槽",
        "- **C** = 同上 shortlist · 盤中輪詢重排 · 空槽依當下排名填入",
        "- kbar 來源：`stock_kbar_1m`（FinMind > Yahoo）",
        "",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG mono hold7 entry A/B/C")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--intraday-minute", default="10:00", help="B 腿盤中定點")
    parser.add_argument("--rebalance-interval-min", type=int, default=5, help="C 腿輪詢間隔")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_hold7_ab_comparison(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            intraday_minute=args.intraday_minute,
            rebalance_interval_min=args.rebalance_interval_min,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_rrg_mono_hold7_intraday_ab.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md_path = args.md or out.with_suffix(".md")
    md_path.write_text(_render_md(payload), encoding="utf-8")
    print(f"Wrote {md_path}")

    for leg_id in ("A", "B", "C"):
        item = payload["legs"][leg_id]
        s = item["summary"]
        print(
            f"{leg_id} {LEG_LABELS[leg_id]}: n={item['n_periods']} "
            f"mean_excess={s.get('mean_excess_pct')}% "
            f"delta_vs_a={item.get('delta_vs_a_pp')}pp "
            f"kbar={s.get('kbar_coverage_pct')}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
