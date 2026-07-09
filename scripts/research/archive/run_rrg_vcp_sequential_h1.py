#!/usr/bin/env python3
"""H1 · RRG 觸發 → N 日內首個 VCP pivot 進場（序列 · hold7）。

用法：
  PYTHONPATH=src python scripts/run_rrg_vcp_sequential_h1.py
  PYTHONPATH=src python scripts/run_rrg_vcp_sequential_h1.py --sweep-lags
  PYTHONPATH=src python scripts/run_rrg_vcp_sequential_h1.py \\
    --date-start 2024-01-01 --max-lag 10 --trigger-gate mono_tier2_new
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_vcp_sequential_h1 import (  # noqa: E402
    run_h1_comparison,
    run_h1_lag_sweep,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _render_comparison_md(payload: dict) -> str:
    trig = payload.get("trigger_stats") or {}
    lines = [
        "# H1 · RRG 觸發 → VCP pivot 序列進場（hold7）",
        "",
        f"區間：{payload['date_start']} .. {payload['date_end']}",
        f"觸發：{payload.get('trigger_gate')} · max_lag={payload.get('max_lag')} 交易日",
        f"觸發事件：{trig.get('trigger_events')}（日均 {trig.get('mean_triggers_per_day')}）",
        "",
        "| 腿 | 說明 | n | 均超額% | 勝率% | vs A (pp) | 備註 |",
        "|----|------|---|---------|-------|-----------|------|",
    ]
    for leg_id, item in payload["legs"].items():
        s = item["summary"]
        note = ""
        if str(leg_id).startswith("H1"):
            note = f"laḡ={s.get('mean_lag_days')}d · fill={s.get('fill_rate_pct')}%"
        lines.append(
            f"| {leg_id} | {item['label']} | {item['n_periods']} "
            f"| {s.get('mean_excess_pct')} | {s.get('win_rate_vs_bench_pct')} "
            f"| {item.get('delta_vs_a_pp')} | {note} |"
        )
    lines += [
        "",
        "## 解讀",
        "",
        "- **A** = RRG mono fresh · 觸發日收盤進場",
        "- **D0** = 僅 VCP pivot · 無 RRG 觸發",
        "- **H1** = RRG 觸發後 max_lag 日內首個 VCP pivot 收盤進場",
        "",
    ]
    return "\n".join(lines) + "\n"


def _render_sweep_md(payload: dict) -> str:
    ref = payload.get("reference") or {}
    lines = [
        "# H1 · max_lag sweep",
        "",
        f"區間：{payload['date_start']} .. {payload['date_end']} · gate={payload.get('trigger_gate')}",
        f"對照 A={ref.get('A_mean_excess_pct')}% · D0={ref.get('D0_mean_excess_pct')}%",
        "",
        "| max_lag | n | 均超額% | 勝率% | laḡ | fill% | vs A (pp) |",
        "|---------|---|---------|-------|------|-------|-----------|",
    ]
    for row in payload.get("lag_sweep") or []:
        lines.append(
            f"| {row['max_lag']} | {row['n_periods']} | {row.get('mean_excess_pct')} "
            f"| {row.get('win_rate_vs_bench_pct')} | {row.get('mean_lag_days')} "
            f"| {row.get('filled_from_trigger_pct')} | {row.get('delta_vs_a_pp')} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="H1 RRG then VCP sequential backtest")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--max-lag", type=int, default=10)
    parser.add_argument(
        "--trigger-gate",
        default="mono_tier2_new",
        choices=("mono_fresh", "mono_tier2_new", "mono_tier2"),
    )
    parser.add_argument("--sweep-lags", action="store_true", help="掃 max_lag 5/10/15/20")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        if args.sweep_lags:
            payload = run_h1_lag_sweep(
                conn,
                date_start=args.date_start,
                date_end=args.date_end,
                trigger_gate=args.trigger_gate,  # type: ignore[arg-type]
            )
        else:
            payload = run_h1_comparison(
                conn,
                date_start=args.date_start,
                date_end=args.date_end,
                max_lag=args.max_lag,
                trigger_gate=args.trigger_gate,  # type: ignore[arg-type]
            )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    suffix = "lag_sweep" if args.sweep_lags else f"h1_lag{args.max_lag}"
    out = args.out or RESEARCH_RRG / f"{stamp}_rrg_vcp_sequential_{suffix}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md_path = args.md or out.with_suffix(".md")
    md_path.write_text(
        _render_sweep_md(payload) if args.sweep_lags else _render_comparison_md(payload),
        encoding="utf-8",
    )
    print(f"Wrote {md_path}")

    if args.sweep_lags:
        for row in payload.get("lag_sweep") or []:
            print(
                f"lag={row['max_lag']} n={row['n_periods']} "
                f"mean_excess={row.get('mean_excess_pct')}% "
                f"delta_vs_a={row.get('delta_vs_a_pp')}pp fill={row.get('filled_from_trigger_pct')}%"
            )
    else:
        for leg_id, item in payload["legs"].items():
            s = item["summary"]
            print(
                f"{leg_id}: n={item['n_periods']} mean_excess={s.get('mean_excess_pct')}% "
                f"delta_vs_a={item.get('delta_vs_a_pp')}pp"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
