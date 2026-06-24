#!/usr/bin/env python3
"""RRG mono hold7 · C 腿變體 sweep。

假說（相對 C0 基線）：
  H-C1 · 輪詢 30m 較 5m 降噪
  H-C2 · shortlist 盤中 full RRG 重算優於 seg_last scale
  H-C3 · confirm_bars=2 連續居前才建倉
  H-C4 · D+1 加速篩選 → D+2 盤中 full RRG 建倉

用法：
  PYTHONPATH=src python scripts/run_rrg_mono_intraday_c_sweep.py
  PYTHONPATH=src python scripts/run_rrg_mono_intraday_c_sweep.py \\
    --date-start 2026-05-24 --date-end 2026-06-22
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_intraday_ab import run_c_variant_sweep  # noqa: E402
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _render_md(payload: dict) -> str:
    lines = [
        "# RRG mono hold7 · C 腿變體 sweep",
        "",
        f"區間：{payload['date_start']} .. {payload['date_end']}",
        f"基線：{payload['baseline_variant_id']}",
        "",
        payload.get("ssg_note", ""),
        "",
        "| rank | id | 說明 | iv | mode | confirm | schedule | n | 均超額% | kbar% | vs基線 |",
        "|------|----|------|----|------|---------|----------|---|---------|-------|--------|",
    ]
    ranked = sorted(
        payload["summaries"],
        key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("n_periods") or 0)),
    )
    for i, s in enumerate(ranked, start=1):
        lines.append(
            f"| {i} | {s.get('variant_id')} | {s.get('label')} "
            f"| {s.get('rebalance_interval_min')} | {s.get('score_mode')} "
            f"| {s.get('confirm_bars')} | {s.get('entry_schedule')} "
            f"| {s.get('n_periods')} | {s.get('mean_excess_pct')} "
            f"| {s.get('kbar_coverage_pct')} | {s.get('delta_vs_baseline_pp')} |"
        )
    best = payload.get("best")
    if best:
        lines += [
            "",
            "## 冠軍",
            "",
            f"```json\n{json.dumps(best, ensure_ascii=False, indent=2)}\n```",
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG mono hold7 C-variant sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--baseline", default="C0")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_c_variant_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            baseline_variant_id=args.baseline,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_rrg_mono_hold7_c_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md_path = args.md or out.with_suffix(".md")
    md_path.write_text(_render_md(payload), encoding="utf-8")
    print(f"Wrote {md_path}")

    best = payload.get("best") or {}
    print(
        f"Best {best.get('variant_id')}: n={best.get('n_periods')} "
        f"mean_excess={best.get('mean_excess_pct')}% "
        f"delta_vs_{args.baseline}={best.get('delta_vs_baseline_pp')}pp"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
