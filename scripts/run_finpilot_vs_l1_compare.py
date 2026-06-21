#!/usr/bin/env python3
"""FinPilot 四策略 vs 00981A L1H9 · 勝台指率對照（H9 · IX0001）。"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.copytrade_backtest import (  # noqa: E402
    compute_win_rate_stats,
    resolve_strategy_specs,
    run_strategies,
)
from research.backtest.finpilot_local_backtest import (  # noqa: E402
    FINPILOT_STRATEGIES,
    run_strategy_h9_periods,
    summarize_periods,
)
from stock_db import connect, DEFAULT_DB_PATH  # noqa: E402


def _l1h9_stats(conn, *, window_start: str | None, window_end: str | None) -> dict:
    specs = resolve_strategy_specs("L1H9", matrix=False, include_l0=False, max_hold=9)
    results = run_strategies(
        conn,
        "00981A",
        capital_ntd=10_000.0,
        strategies=specs,
        window_start=window_start,
        window_end=window_end,
        persist=False,
    )
    r = results[0]
    wr = compute_win_rate_stats(r.signal_days)
    complete = [d for d in r.signal_days if d.status == "complete"]
    return {
        "strategy_id": r.strategy_id,
        "n_signal_days": len(complete),
        "win_rate_gross_pct": wr["win_rate_gross_pct"],
        "win_rate_vs_bench_pct": wr["win_rate_vs_bench_pct"],
        "window_start": min(d.signal_date for d in complete) if complete else None,
        "window_end": max(d.signal_date for d in complete) if complete else None,
    }


def format_report(l1: dict, rows: list[dict], *, hold_days: int) -> str:
    lines = [
        "# FinPilot 動能策略 vs 00981A L1H9 · 勝台指率對照",
        "",
        "> 來源：[hu0937/FinPilot](https://github.com/hu0937/FinPilot) 策略邏輯本地重現",
        "> 資料：本專案 `stock_daily_bars` + `stock_fundamental`（FinMind 成分股聯集）",
        f"> 執行：月頻選股（月末訊號）→ 次月首交易日開盤進場 → **H{hold_days}** 收盤出場",
        "> L1H9：00981A 持股變化訊號日 → T+1 開盤買 → H9 收盤賣",
        "> 基準：**IX0001** 同期間報酬",
        "",
        "## L1H9 基準",
        "",
        f"| 策略 | n | 勝率（毛） | **勝台指%** | 期間 |",
        f"|------|---|-----------|------------|------|",
        f"| L1H9 | {l1['n_signal_days']} | {l1['win_rate_gross_pct']}% | "
        f"**{l1['win_rate_vs_bench_pct']}%** | {l1['window_start']} → {l1['window_end']} |",
        "",
        f"## FinPilot 策略（H{hold_days} 對齊）",
        "",
        "### 全樣本",
        "",
        "| ID | 策略 | n | 勝率（毛） | **勝台指%** | 均報酬% | 期間 |",
        "|----|------|---|-----------|------------|---------|------|",
    ]
    for row in rows:
        full = row["full"]
        lines.append(
            f"| {row['id']} | {row['label']} | {full['n_periods']} | "
            f"{full['win_rate_gross_pct']}% | **{full['win_rate_vs_bench_pct']}%** | "
            f"{full.get('mean_return_pct', '—')} | "
            f"{full['window_start']} → {full['window_end']} |"
        )

    lines.extend(
        [
            "",
            f"### L1 重疊區間（{l1['window_start']} → {l1['window_end']}）",
            "",
            "| ID | 策略 | n | **勝台指%** | Δ vs L1 | 均報酬% |",
            "|----|------|---|------------|---------|---------|",
            f"| — | **L1H9** | {l1['n_signal_days']} | **{l1['win_rate_vs_bench_pct']}%** | — | — |",
        ]
    )
    l1_wr = float(l1["win_rate_vs_bench_pct"] or 0)
    for row in rows:
        ov = row["overlap"]
        wr = float(ov["win_rate_vs_bench_pct"] or 0)
        delta = round(wr - l1_wr, 2)
        lines.append(
            f"| {row['id']} | {row['label']} | {ov['n_periods']} | "
            f"**{ov['win_rate_vs_bench_pct']}%** | {delta:+.2f} pp | "
            f"{ov.get('mean_return_pct', '—')} |"
        )

    best = max(
        (r for r in rows if r["overlap"]["win_rate_vs_bench_pct"] is not None),
        key=lambda r: float(r["overlap"]["win_rate_vs_bench_pct"]),
        default=None,
    )
    lines.extend(["", "## 解讀", ""])
    if best:
        bwr = best["overlap"]["win_rate_vs_bench_pct"]
        lines.append(
            f"- 重疊區間勝台指最高：**{best['id']} {best['label']}**（{bwr}%）。"
        )
    lines.append(
        f"- L1H9 勝台指 **{l1['win_rate_vs_bench_pct']}%**；"
        "FinPilot 原版用 FinLab 全市場 + 月頻持有至下月，此處改 H9 對標。"
    )
    lines.append(
        "- 本地重現僅涵蓋 DB 內成分股聯集（~135 檔），非 FinLab 全市場；"
        "若設 `FINLAB_API_TOKEN` 可改用 FinLab 原版回測。"
    )
    lines.append("")
    lines.append("```bash")
    lines.append(
        "PYTHONPATH=src .venv/bin/python scripts/run_finpilot_vs_l1_compare.py --write-report"
    )
    lines.append("```")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="FinPilot vs L1H9 勝台指率對照")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--hold-days", type=int, default=9)
    parser.add_argument(
        "--strategies",
        default="s01,s04,s05,s06",
        help="逗號分隔：s01,s04,s05,s06",
    )
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()

    wanted = {s.strip() for s in args.strategies.split(",") if s.strip()}
    specs = [s for s in FINPILOT_STRATEGIES if s.strategy_id in wanted]
    if not specs:
        print("無有效策略 ID")
        return 1

    conn = connect(args.db)
    l1 = _l1h9_stats(conn, window_start=None, window_end=None)
    rows: list[dict] = []
    for spec in specs:
        print(f"回測 {spec.strategy_id} {spec.label}…")
        periods = run_strategy_h9_periods(
            conn, spec.strategy_id, hold_days=args.hold_days
        )
        full = summarize_periods(periods)
        overlap_periods = [
            p
            for p in periods
            if l1["window_start"]
            and l1["window_end"]
            and l1["window_start"] <= p["entry_date"] <= l1["window_end"]
        ]
        overlap = summarize_periods(overlap_periods)
        rows.append(
            {
                "id": spec.strategy_id,
                "label": spec.label,
                "file": spec.finpilot_file,
                "full": full,
                "overlap": overlap,
            }
        )
    conn.close()

    report = format_report(l1, rows, hold_days=args.hold_days)
    print(report)
    if args.write_report:
        out = ROOT / "reports" / f"{date.today().strftime('%Y%m%d')}_finpilot_vs_l1h9.md"
        out.write_text(report, encoding="utf-8")
        print(f"已寫入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
