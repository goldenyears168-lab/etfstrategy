#!/usr/bin/env python3
"""Standalone expert intraday entry backtest · 非日線訊號疊加。

對照五種專家盤中進場（VWAP reclaim/bounce · Bone Zone · ORB · Pivot retest）
於 ETF 成分 kbar universe · hold 5 日收盤出場 · IX0001 對照基準。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.expert_intraday_standalone import (  # noqa: E402
    DEFAULT_HOLD_DAYS,
    STANDALONE_MODES,
    run_standalone_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

DATE_START = "2024-01-01"
DATE_END = "2026-06-22"
OUT_DIR = ROOT / "reports" / "research" / "intraday"


def _markdown_summary(payload: dict) -> str:
    lines = [
        "# Expert intraday standalone backtest",
        "",
        f"- Window: {payload['date_start']} → {payload['date_end']}",
        f"- Exit: {payload['exit_rule']}",
        f"- Benchmark: {payload['benchmark']}",
        f"- Universe: {payload['universe_note']} (n={payload['universe_size']})",
        f"- Trade days: {payload['trade_days']}",
        "",
        "## Per-strategy summary",
        "",
        "| Strategy | n | Win rate vs bench | Mean return | Mean excess | Max DD | Stopped |",
        "|----------|---|-------------------|-------------|-------------|--------|---------|",
    ]
    for v in payload["variants"]:
        s = v["summary"]
        lines.append(
            f"| {v['strategy_id']} | {s.get('n_periods', 0)} | "
            f"{s.get('win_rate_vs_bench_pct')}% | {s.get('mean_return_pct')}% | "
            f"{s.get('mean_excess_pct')}% | {s.get('max_drawdown_pct')}% | "
            f"{s.get('n_stopped', 0)} |"
        )
    bench = payload.get("bench_do_nothing") or {}
    lines.extend(
        [
            "",
            "## Buy-and-hold benchmark (IX0001)",
            "",
            f"- {bench.get('label')}: mean {bench.get('mean_bench_pct')}% "
            f"(n={bench.get('n_windows')})",
            "",
            "## Interpretation",
            "",
            "Positive mean excess vs IX0001 over the same hold window suggests edge; "
            "compare win rate vs bench and max drawdown across strategies.",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Standalone expert intraday backtest")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default=DATE_START)
    parser.add_argument("--date-end", default=DATE_END)
    parser.add_argument("--hold-days", type=int, default=DEFAULT_HOLD_DAYS)
    parser.add_argument("--min-stock-days", type=int, default=30)
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument("--out-md", type=Path, default=None)
    parser.add_argument(
        "--modes",
        nargs="*",
        default=list(STANDALONE_MODES),
        choices=list(STANDALONE_MODES),
    )
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_standalone_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            modes=tuple(args.modes),
            hold_days=args.hold_days,
            min_stock_days=args.min_stock_days,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_json = args.out_json or OUT_DIR / f"{stamp}_expert_intraday_standalone.json"
    out_md = args.out_md or OUT_DIR / f"{stamp}_expert_intraday_standalone.md"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_markdown_summary(payload), encoding="utf-8")
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(f"Universe: {payload['universe_size']} stocks · {payload['trade_days']} days")
    for v in payload["variants"]:
        s = v["summary"]
        print(
            f"  {v['strategy_id']:<14} n={s.get('n_periods'):<5} "
            f"win={s.get('win_rate_vs_bench_pct')}% "
            f"ret={s.get('mean_return_pct')}% "
            f"ex={s.get('mean_excess_pct')}% "
            f"dd={s.get('max_drawdown_pct')}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
