#!/usr/bin/env python3
"""L2b Mom60 vs L1c Mom20 vs L1H9 · 2025-05 起逐月對照（月頻 · H9）。"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.copytrade_backtest import (  # noqa: E402
    resolve_strategy_specs,
    run_strategies,
)
from research.backtest.finpilot_s04_layers import (  # noqa: E402
    S04_LAYER_SPECS,
    run_s04_layer_periods,
    summarize_by_month,
    summarize_periods,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _l1h9_by_month(
    conn,
    *,
    month_start: str,
    month_end: str,
) -> tuple[list[dict], list[dict]]:
    specs = resolve_strategy_specs("L1H9", matrix=False, include_l0=False, max_hold=9)
    results = run_strategies(
        conn, "00981A", capital_ntd=10_000.0, strategies=specs, persist=False
    )
    complete = [d for d in results[0].signal_days if d.status == "complete"]
    detail: list[dict] = []
    for d in complete:
        ym = d.signal_date[:7]
        if ym < month_start or ym > month_end:
            continue
        detail.append(
            {
                "month": ym,
                "signal_date": d.signal_date,
                "return_pct": d.return_pct,
                "bench_return_pct": d.bench_return_pct,
                "excess_pct": d.return_pct - d.bench_return_pct,
                "beat_bench": d.return_pct > d.bench_return_pct,
            }
        )
    detail.sort(key=lambda r: (r["month"], r["signal_date"]))
    agg: list[dict] = []
    if detail:
        import pandas as pd

        for month, grp in pd.DataFrame(detail).groupby("month", sort=True):
            sub = grp.to_dict("records")
            n = len(sub)
            agg.append(
                {
                    "month": month,
                    "n_signal_days": n,
                    "win_rate_vs_bench_pct": round(
                        sum(1 for r in sub if r["beat_bench"]) / n * 100, 2
                    ),
                    "mean_return_pct": round(
                        sum(r["return_pct"] for r in sub) / n, 4
                    ),
                    "mean_excess_pct": round(
                        sum(r["excess_pct"] for r in sub) / n, 4
                    ),
                }
            )
    return agg, detail


def _s04_monthly(
    conn,
    *,
    layer_id: str,
    mom_lookback: int,
    hold_days: int,
    month_start: str,
    month_end: str,
) -> list[dict]:
    spec = next(s for s in S04_LAYER_SPECS if s.layer_id == layer_id)
    periods = run_s04_layer_periods(
        conn, spec, hold_days=hold_days, mom_lookback=mom_lookback
    )
    return summarize_by_month(
        periods, month_start=month_start, month_end=month_end
    )


def _period_summary(rows: list[dict]) -> dict:
    return summarize_periods(
        [
            {
                "return_pct": r["return_pct"],
                "bench_return_pct": r["bench_return_pct"],
                "beat_bench": r["beat_bench"],
                "gross_win": r["gross_win"],
                "entry_date": r["entry_date"],
            }
            for r in rows
        ]
    )


def format_report(
    *,
    mom60_rows: list[dict],
    mom20_rows: list[dict],
    l1_agg: list[dict],
    l1_detail: list[dict],
    month_start: str,
    month_end: str,
    hold_days: int,
) -> str:
    m60 = {r["month"]: r for r in mom60_rows}
    m20 = {r["month"]: r for r in mom20_rows}
    l1 = {r["month"]: r for r in l1_agg}
    months = sorted(set(m60) | set(m20) | set(l1))

    lines = [
        "# L2b Mom60 vs L1c Mom20 vs L1H9 · 逐月對照",
        "",
        f"> **L2b Mom60**：Top30 + ROE>0 · **L1c Mom20**：Top30 無 ROE · **月頻** · H{hold_days}",
        f"> L1H9：00981A 跟單 · T+1 開盤 · 等權 1 萬/訊號日",
        f"> 區間：**{month_start}** → **{month_end}**（00981A 上市後）· 基準 IX0001",
        "",
        "## 逐月",
        "",
        "| 月 | L2b Mom60 超額% | 勝 | L1c Mom20 超額% | 勝 | L1H9 n | L1H9 勝台指% | L1H9 均超額% |",
        "|----|-----------------|-----|------------------|-----|--------|-------------|-------------|",
    ]

    for ym in months:
        r60 = m60.get(ym)
        r20 = m20.get(ym)
        l1m = l1.get(ym)

        def _cell(row: dict | None, key: str) -> str:
            if row is None:
                return "—"
            return f"{row[key]:.2f}"

        def _win(row: dict | None) -> str:
            if row is None:
                return "—"
            return "✅" if row["beat_bench"] else "❌"

        lines.append(
            f"| {ym} | {_cell(r60, 'excess_pct')} | {_win(r60)} | "
            f"{_cell(r20, 'excess_pct')} | {_win(r20)} | "
            f"{l1m['n_signal_days'] if l1m else '—'} | "
            f"{l1m['win_rate_vs_bench_pct'] if l1m else '—'}% | "
            f"{l1m['mean_excess_pct'] if l1m else '—'} |"
        )

    overlap_months = [ym for ym in months if ym in m60 and ym in m20 and ym in l1]
    if overlap_months:
        mom60_ov = [m60[ym] for ym in overlap_months]
        mom20_ov = [m20[ym] for ym in overlap_months]
        l1_ov_detail = [d for d in l1_detail if d["month"] in overlap_months]

        s60 = _period_summary(mom60_ov)
        s20 = _period_summary(mom20_ov)
        l1_n = len(l1_ov_detail)
        l1_wr_pct = round(
            sum(1 for d in l1_ov_detail if d["beat_bench"]) / l1_n * 100, 2
        )
        l1_mean_ex = round(
            sum(d["excess_pct"] for d in l1_ov_detail) / l1_n, 4
        )

        lines.extend(
            [
                "",
                f"## 重疊摘要（{overlap_months[0]} → {overlap_months[-1]} · {len(overlap_months)} 月）",
                "",
                "| 策略 | n | 勝台指% | 均超額% |",
                "|------|---|---------|---------|",
                f"| L2b Mom60 月頻 | {s60['n_periods']} | {s60['win_rate_vs_bench_pct']}% | "
                f"{round(sum(r['excess_pct'] for r in mom60_ov) / len(mom60_ov), 4)} |",
                f"| L1c Mom20 月頻 | {s20['n_periods']} | {s20['win_rate_vs_bench_pct']}% | "
                f"{round(sum(r['excess_pct'] for r in mom20_ov) / len(mom20_ov), 4)} |",
                f"| L1H9 | {l1_n} | {l1_wr_pct}% | {l1_mean_ex} |",
            ]
        )

    lines.extend(
        [
            "",
            "## 解讀",
            "",
            "- **月頻 s04**：每月末訊號 → 次月開盤進 → H9 出（每月 1 筆）。",
            "- **L1H9**：同一月可有多訊號日；勝台指% 為該月訊號日勝率。",
            "- L1c Mom20 取自日頻 sweep 重疊區最佳組合之一（Mom20 無 ROE）。",
            "",
            "```bash",
            "PYTHONPATH=src .venv/bin/python scripts/run_s04_monthly_tri_compare.py --write-report",
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="L2b Mom60 vs L1c Mom20 vs L1H9 monthly"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--hold-days", type=int, default=9)
    parser.add_argument("--month-start", default="2025-05")
    parser.add_argument("--month-end", default="2026-12")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()

    conn = connect(args.db)
    print("L2b Mom60 月頻…")
    mom60_rows = _s04_monthly(
        conn,
        layer_id="L2b",
        mom_lookback=60,
        hold_days=args.hold_days,
        month_start=args.month_start,
        month_end=args.month_end,
    )
    print(f"  {len(mom60_rows)} 月")

    print("L1c Mom20 月頻…")
    mom20_rows = _s04_monthly(
        conn,
        layer_id="L1c",
        mom_lookback=20,
        hold_days=args.hold_days,
        month_start=args.month_start,
        month_end=args.month_end,
    )
    print(f"  {len(mom20_rows)} 月")

    print("L1H9…")
    l1_agg, l1_detail = _l1h9_by_month(
        conn, month_start=args.month_start, month_end=args.month_end
    )
    print(f"  {len(l1_agg)} 月 · {len(l1_detail)} 訊號日")
    conn.close()

    report = format_report(
        mom60_rows=mom60_rows,
        mom20_rows=mom20_rows,
        l1_agg=l1_agg,
        l1_detail=l1_detail,
        month_start=args.month_start,
        month_end=args.month_end,
        hold_days=args.hold_days,
    )
    print(report)

    if args.write_report:
        out = (
            ROOT
            / "reports"
            / f"{date.today().strftime('%Y%m%d')}_s04_l2b60_l1c20_vs_l1h9_monthly.md"
        )
        out.write_text(report, encoding="utf-8")
        print(f"已寫入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
