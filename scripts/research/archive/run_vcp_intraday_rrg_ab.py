#!/usr/bin/env python3
"""H3 · 日線 VCP pivot 池 + 盤中 RRG 重排 · hold7 對照。

D0 · VCP 池 · 收盤 composite 填槽
Db · VCP 池前十 → 盤中定點 seg_last 重排
D  · VCP 池前十 → 盤中輪詢 seg_last 重排

用法：
  PYTHONPATH=src python scripts/run_vcp_intraday_rrg_ab.py
  PYTHONPATH=src python scripts/run_vcp_intraday_rrg_ab.py \\
    --date-start 2025-12-01 --date-end 2026-06-22
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
    VCP_LEG_LABELS,
    run_vcp_intraday_rrg_comparison,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _render_md(payload: dict) -> str:
    ref = payload.get("reference_rrg_mono_a") or {}
    pool = payload.get("pool_stats") or {}
    gate = payload.get("vcp_gate") or {}
    lines = [
        "# H3 · 日線 VCP pivot 池 + 盤中 RRG 重排（hold7）",
        "",
        f"區間：{payload['date_start']} .. {payload['date_end']}",
        f"Db 盤中定點：{payload['intraday_minute']} · D 輪詢間隔：{payload['rebalance_interval_min']} 分",
        "",
        f"VCP gate：composite≥{gate.get('min_composite')} · states={gate.get('execution_states')} · "
        f"樞紐 {gate.get('min_dist_pivot_pct')}%～{gate.get('max_dist_pivot_pct')}%",
        "",
        f"池覆蓋：{pool.get('days_with_candidates')} 日有候選 · "
        f"均 {pool.get('mean_pool_size')} 檔 · max {pool.get('max_pool_size')}",
        "",
        f"對照 RRG mono **A** 腿：n={ref.get('n_periods')} · 均超額 {ref.get('mean_excess_pct')}%",
        "",
        "| 腿 | 說明 | n | 均超額% | 勝率% | kbar% | vs D0 (pp) |",
        "|----|------|---|---------|-------|-------|------------|",
    ]
    for leg_id in ("D0", "Db", "D"):
        item = payload["legs"][leg_id]
        s = item["summary"]
        lines.append(
            f"| {leg_id} | {item['label']} | {item['n_periods']} "
            f"| {s.get('mean_excess_pct')} | {s.get('win_rate_vs_bench_pct')} "
            f"| {s.get('kbar_coverage_pct')} | {item.get('delta_vs_d0_pp')} |"
        )
    lines += [
        "",
        "## 解讀",
        "",
        "- **D0** = VCP 日線池 · 收盤依 composite 進場（H3 基線）",
        "- **Db** = 同池 · 單一盤中時點依 seg_last×價格 scale 重排",
        "- **D** = 同池 · 盤中輪詢 seg_last 重排",
        "- 出場均 hold7 · 3 槽 · 與 RRG mono A 腿交叉對照",
        "",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="H3 VCP pool + intraday RRG rank")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2025-12-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--intraday-minute", default="10:00")
    parser.add_argument("--rebalance-interval-min", type=int, default=5)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_vcp_intraday_rrg_comparison(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            intraday_minute=args.intraday_minute,
            rebalance_interval_min=args.rebalance_interval_min,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_vcp_intraday_rrg_h3.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md_path = args.md or out.with_suffix(".md")
    md_path.write_text(_render_md(payload), encoding="utf-8")
    print(f"Wrote {md_path}")

    for leg_id in ("D0", "Db", "D"):
        item = payload["legs"][leg_id]
        s = item["summary"]
        print(
            f"{leg_id} {VCP_LEG_LABELS[leg_id]}: n={item['n_periods']} "
            f"mean_excess={s.get('mean_excess_pct')}% "
            f"delta_vs_d0={item.get('delta_vs_d0_pp')}pp "
            f"kbar={s.get('kbar_coverage_pct')}%"
        )
    ref = payload.get("reference_rrg_mono_a") or {}
    print(f"RRG mono A ref: mean_excess={ref.get('mean_excess_pct')}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
