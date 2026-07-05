#!/usr/bin/env python3
"""C18acc × Extension radar · 持倉提早出場 overlay sweep。

用法：
  PYTHONPATH=src python scripts/run_c18acc_extension_exit_sweep.py
  PYTHONPATH=src python scripts/run_c18acc_extension_exit_sweep.py \\
    --date-start 2026-01-01 --date-end 2026-06-26
  PYTHONPATH=src python scripts/run_c18acc_extension_exit_sweep.py \\
    --case-study 3711 --case-date 2026-06-26
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.c18acc_extension_exit import (  # noqa: E402
    case_study_session,
    run_c18acc_extension_exit_sweep,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _render_md(payload: dict) -> str:
    lines = [
        "# C18acc × Extension radar · 提早出場 overlay sweep",
        "",
        f"區間：{payload['date_start']} .. {payload['date_end']}",
        f"Champion：{payload.get('champion_variant')}",
        "",
        "## 假說",
        "",
    ]
    for k, v in (payload.get("hypotheses") or {}).items():
        lines.append(f"- **{k}**：{v}")
    base = payload.get("baseline_summary") or {}
    lines += [
        "",
        f"基線 C18acc：n={base.get('n_periods')} · 均超額 {base.get('mean_excess_pct')}%",
        "",
        "| id | 模式 | triggers | n | 均超額% | Δ vs X0 | median save |",
        "|----|------|----------|---|---------|---------|-------------|",
    ]
    for s in payload.get("summaries") or []:
        lines.append(
            f"| {s.get('variant_id')} | {s.get('exit_mode')} "
            f"| {s.get('overlay_triggers')} | {s.get('n_periods')} "
            f"| {s.get('mean_excess_pct')} | {s.get('delta_vs_baseline_pp')} "
            f"| {s.get('median_save_vs_orig_pct')} |"
        )
    best = payload.get("best") or {}
    if best:
        lines += [
            "",
            "## 最佳（依均超額）",
            "",
            f"- **{best.get('variant_id')}** · {best.get('label')}",
            f"- triggers={best.get('overlay_triggers')} · 均超額 {best.get('mean_excess_pct')}%",
        ]
    plan = payload.get("research_plan") or ""
    if plan:
        lines += ["", "---", "", plan]
    lines += [
        "",
        "---",
        "模組：`scripts/run_c18acc_extension_exit_sweep.py`",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc extension exit overlay sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-26")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    parser.add_argument("--case-study", default="", help="單日 case · stock_id")
    parser.add_argument("--case-date", default="", help="YYYY-MM-DD")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        if args.case_study and args.case_date:
            cs = case_study_session(
                conn, stock_id=args.case_study, trade_date=args.case_date
            )
            print(json.dumps(cs, ensure_ascii=False, indent=2))
            return 0

        payload = run_c18acc_extension_exit_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_c18acc_extension_exit_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md_path = args.md or out.with_suffix(".md")
    md_path.write_text(_render_md(payload), encoding="utf-8")
    print(f"Wrote {md_path}")

    best = payload.get("best") or {}
    print(
        f"Best {best.get('variant_id')}: triggers={best.get('overlay_triggers')} "
        f"mean_excess={best.get('mean_excess_pct')} "
        f"delta={best.get('delta_vs_baseline_pp')}pp"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
