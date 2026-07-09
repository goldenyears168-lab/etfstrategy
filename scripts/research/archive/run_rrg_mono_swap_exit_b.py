#!/usr/bin/env python3
"""RRG mono hold7 · 模式 B sweep：左下 + 有更強 challenger 才換倉。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_swap_exit_b import run_swap_exit_b_sweep  # noqa: E402
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _render_md(payload: dict) -> str:
    ref = payload.get("reference_hold7") or {}
    lines = [
        "# RRG mono hold7 · 模式 B（左下 + challenger 換倉）",
        "",
        f"區間：{payload['date_start']} .. {payload['date_end']}",
        "",
        payload.get("ssg_note", ""),
        "",
        f"**hold7 對照**：n={ref.get('n_periods')} · 均超額 {ref.get('mean_excess_pct')}%",
        "",
        "| rank | id | gate | beat | min | n | swaps | 均超額% | 均持有日 | vs hold7 |",
        "|------|----|------|------|-----|---|-------|---------|----------|----------|",
    ]
    ranked = sorted(
        payload["summaries"],
        key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("n_periods") or 0)),
    )
    for i, s in enumerate(ranked, start=1):
        lines.append(
            f"| {i} | {s.get('variant_id')} | {s.get('structural_gate', '-')} "
            f"| {s.get('challenger_beat', '-')} | {s.get('min_hold_days', '-')} "
            f"| {s.get('n_periods')} | {s.get('swaps_total')} "
            f"| {s.get('mean_excess_pct')} | {s.get('mean_hold_days')} "
            f"| {s.get('delta_vs_hold7_pp')} |"
        )
    best = payload.get("best")
    if best:
        lines += [
            "",
            "## 冠軍",
            "",
            f"- **{best.get('variant_id')}** · {best.get('label')}",
            f"- swaps={best.get('swaps_total')} · 均超額 {best.get('mean_excess_pct')}%",
            "",
        ]
    lines += ["---", "模組：`scripts/run_rrg_mono_swap_exit_b.py`", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG mono swap exit mode B")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2026-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_swap_exit_b_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_rrg_mono_swap_exit_b.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md_path = args.md or out.with_suffix(".md")
    md_path.write_text(_render_md(payload), encoding="utf-8")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
