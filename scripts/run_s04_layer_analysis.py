#!/usr/bin/env python3
"""s04 60日動能+ROE>0 分層拆解 · 延長回測 · vs L1H9。"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.copytrade_backtest import (  # noqa: E402
    compute_win_rate_stats,
    resolve_strategy_specs,
    run_strategies,
)
from research.backtest.finpilot_s04_layers import (  # noqa: E402
    S04_LAYER_SPECS,
    aggregate_monthly_stats,
    roe_filter_marginal_summary,
    run_s04_layer_periods,
    summarize_by_month,
    summarize_by_year,
    summarize_periods,
)
from stock_db import (  # noqa: E402
    DEFAULT_DB_PATH,
    connect,
    upsert_stock_financial_history,
)


def _l1h9_by_month(
    conn,
    *,
    month_start: str = "2025-01",
    month_end: str = "2026-12",
) -> tuple[list[dict], list[dict]]:
    """L1H9 依訊號月彙總 + 逐月明細。"""
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
                "gross_win": d.pnl_ntd > 0,
            }
        )
    detail.sort(key=lambda r: (r["month"], r["signal_date"]))
    agg: list[dict] = []
    if detail:
        df = __import__("pandas").DataFrame(detail)
        for month, grp in df.groupby("month", sort=True):
            sub = grp.to_dict("records")
            n = len(sub)
            agg.append(
                {
                    "month": month,
                    "n_signal_days": n,
                    "win_rate_vs_bench_pct": round(
                        sum(1 for r in sub if r["beat_bench"]) / n * 100, 2
                    ),
                    "win_rate_gross_pct": round(
                        sum(1 for r in sub if r["gross_win"]) / n * 100, 2
                    ),
                    "mean_return_pct": round(
                        sum(r["return_pct"] for r in sub) / n, 4
                    ),
                    "mean_bench_pct": round(
                        sum(r["bench_return_pct"] for r in sub) / n, 4
                    ),
                    "mean_excess_pct": round(
                        sum(r["excess_pct"] for r in sub) / n, 4
                    ),
                }
            )
    return agg, detail


def _l1h9_overlap(conn) -> dict:
    specs = resolve_strategy_specs("L1H9", matrix=False, include_l0=False, max_hold=9)
    results = run_strategies(
        conn, "00981A", capital_ntd=10_000.0, strategies=specs, persist=False
    )
    r = results[0]
    wr = compute_win_rate_stats(r.signal_days)
    complete = [d for d in r.signal_days if d.status == "complete"]
    return {
        "n": len(complete),
        "win_rate_vs_bench_pct": wr["win_rate_vs_bench_pct"],
        "window_start": min(d.signal_date for d in complete) if complete else None,
        "window_end": max(d.signal_date for d in complete) if complete else None,
    }


def backfill_financial_history(
    conn,
    *,
    lookback_days: int,
    request_delay: float = 0.35,
) -> int:
    """延長 stock_financial_history（FinMind 季報 ROE 用）。"""
    from sync_fundamentals import build_stock_fundamentals

    end = date.today()
    start = end - timedelta(days=lookback_days)
    stock_ids = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT stock_id FROM stock_daily_bars ORDER BY stock_id"
        ).fetchall()
    ]
    total = 0
    print(f"回補財報 {start} → {end}，{len(stock_ids)} 檔…")
    for i, stock_id in enumerate(stock_ids):
        if i > 0 and request_delay > 0:
            time.sleep(request_delay)
        try:
            _fund, history, _cons = build_stock_fundamentals(stock_id, start, end)
            if history:
                total += upsert_stock_financial_history(conn, history)
        except Exception as exc:
            print(f"  WARN {stock_id}: {exc}", file=sys.stderr)
    conn.commit()
    print(f"  寫入 history rows（累計 upsert 次數）: {total}")
    return total


def format_report(
    layer_rows: list[dict],
    *,
    l1: dict,
    hold_days: int,
    fund_range: tuple[str | None, str | None],
    price_range: tuple[str | None, str | None],
    l1_monthly_agg: list[dict],
    l1_monthly_detail: list[dict],
    month_start: str,
    month_end: str,
) -> str:
    full_row = next((r for r in layer_rows if r["layer_id"] == "L2b"), None)
    lines = [
        "# s04 60日動能+ROE>0 · 分層拆解與延長回測",
        "",
        "> 來源：FinPilot `s04_momentum_roe.py` · 本地 FinMind 重現",
        f"> 價格區間：**{price_range[0]} → {price_range[1]}**",
        f"> 財報區間：**{fund_range[0]} → {fund_range[1]}**（ROE 層 PIT）",
        f"> 執行：月末訊號 → 次月開盤進 → **H{hold_days}** 收盤出 · 基準 IX0001",
        "",
        "## 策略分層架構",
        "",
        "```",
        "Universe（DB 成分股聯集 ~135 檔）",
        "    ↓ Layer 1 · 動能 Mom60 = close / close.shift(60)",
        "    Top-N 排名（N=10/20/30）",
        "    ↓ Layer 2 · 品質 ROE > 0（季報 PIT：NI/Equity）",
        "    s04 完整：先取 Top30 動能，再剔除 ROE≤0",
        "    ↓ Layer 3 · 組合 等權 basket",
        "    ↓ Layer 4 · 執行 H9（T+1 open → H9 close）",
        "```",
        "",
        "## 分層 ablation（全樣本）",
        "",
        "| 層 | 變體 | n | 勝率% | 勝率（毛） | 均報酬% | 均超額% | 期間 |",
        "|----|------|---|---------|-----------|---------|---------|------|",
    ]
    for r in layer_rows:
        s = r["summary"]
        mean_ex = None
        if r["periods"]:
            mean_ex = round(
                sum(p["excess_pct"] for p in r["periods"]) / len(r["periods"]), 4
            )
        lines.append(
            f"| {r['layer_id']} | {r['label']} | {s['n_periods']} | "
            f"**{s['win_rate_vs_bench_pct']}%** | {s['win_rate_gross_pct']}% | "
            f"{s.get('mean_return_pct', '—')} | {mean_ex if mean_ex is not None else '—'} | "
            f"{s['window_start']} → {s['window_end']} |"
        )

    if full_row and l1.get("window_start"):
        lines.extend(
            [
                "",
                f"## vs L1H9（重疊 {l1['window_start']} → {l1['window_end']}）",
                "",
                "| 策略 | n | 勝率% | Δ vs L1 |",
                "|------|---|---------|---------|",
                f"| L1H9 | {l1['n']} | **{l1['win_rate_vs_bench_pct']}%** | — |",
            ]
        )
        l1_wr = float(l1["win_rate_vs_bench_pct"] or 0)
        for r in layer_rows:
            ov = r["overlap"]
            if ov["n_periods"] == 0:
                continue
            wr = float(ov["win_rate_vs_bench_pct"] or 0)
            lines.append(
                f"| {r['layer_id']} {r['label']} | {ov['n_periods']} | "
                f"**{ov['win_rate_vs_bench_pct']}%** | {wr - l1_wr:+.2f} pp |"
            )

    if full_row:
        lines.extend(["", "## ROE 濾網邊際（L2b 完整 s04）", ""])
        m = full_row["roe_marginal"]
        lines.append(
            f"- 有剔除 ROE 的期數：**{m['n_periods_with_drops']}** / {full_row['summary']['n_periods']}"
        )
        if m["n_periods_with_drops"]:
            lines.append(f"- 平均每月被 ROE 剔除：**{m['avg_dropped_n']}** 檔")
            lines.append(
                f"- 被剔除組 H9 均報酬：**{m['dropped_mean_ret']}%** · "
                f"勝率 {m['dropped_beat_bench_pct']}%"
            )
            lines.append(f"- 保留組 H9 均報酬：**{m['kept_mean_ret']}%**")
            if (
                m["dropped_mean_ret"] is not None
                and m["kept_mean_ret"] is not None
                and m["dropped_mean_ret"] < m["kept_mean_ret"]
            ):
                lines.append("- **解讀**：ROE>0 濾網剔除的部位平均表現較差 → 濾網有正向邊際。")
            else:
                lines.append("- **解讀**：ROE 濾網邊際在此樣本不明顯或為負向，需分年檢視。")

        lines.extend(["", "### L2b 逐年勝率%", "", "| 年 | n | 勝率% | 均報酬% |", "|----|---|---------|---------|"])
        for y in full_row["by_year"]:
            lines.append(
                f"| {y['year']} | {y['n_periods']} | {y['win_rate_vs_bench_pct']}% | "
                f"{y.get('mean_return_pct', '—')} |"
            )

        s04_months = summarize_by_month(
            full_row["periods"], month_start=month_start, month_end=month_end
        )
        l1_by_month = {r["month"]: r for r in l1_monthly_agg}
        all_months = sorted(
            set(m["month"] for m in s04_months)
            | set(l1_by_month.keys())
        )
        lines.extend(
            [
                "",
                f"## {month_start[:4]}–{month_end[:4]} 逐月對照（L2b s04 vs L1H9）",
                "",
                "| 月 | L2b 進場 | n股 | 組合% | 台指% | 超額% | 勝率 | "
                "L1H9 n | L1H9 勝率% | L1H9 均報酬% |",
                "|----|----------|-----|-------|-------|-------|--------|"
                "---------|-------------|-------------|",
            ]
        )
        for ym in all_months:
            s04 = next((m for m in s04_months if m["month"] == ym), None)
            l1m = l1_by_month.get(ym)
            if s04:
                beat = "✅" if s04["beat_bench"] else "❌"
                lines.append(
                    f"| {ym} | {s04['entry_date']} | {s04['n_stocks']} | "
                    f"{s04['return_pct']:.2f} | {s04['bench_return_pct']:.2f} | "
                    f"{s04['excess_pct']:.2f} | {beat} | "
                    f"{l1m['n_signal_days'] if l1m else '—'} | "
                    f"{l1m['win_rate_vs_bench_pct'] if l1m else '—'}% | "
                    f"{l1m['mean_return_pct'] if l1m else '—'} |"
                )
            elif l1m:
                lines.append(
                    f"| {ym} | — | — | — | — | — | — | "
                    f"{l1m['n_signal_days']} | {l1m['win_rate_vs_bench_pct']}% | "
                    f"{l1m['mean_return_pct']} |"
                )

        lines.extend(
            [
                "",
                f"### L1H9 訊號日明細（{month_start} → {month_end}）",
                "",
                "| 訊號日 | 組合% | 台指% | 超額% | 勝率 |",
                "|--------|-------|-------|-------|--------|",
            ]
        )
        for d in l1_monthly_detail:
            beat = "✅" if d["beat_bench"] else "❌"
            lines.append(
                f"| {d['signal_date']} | {d['return_pct']:.2f} | "
                f"{d['bench_return_pct']:.2f} | {d['excess_pct']:.2f} | {beat} |"
            )

    lines.extend(
        [
            "",
            "## 解讀要點",
            "",
            "- **L1（純動能）**：檢驗 Mom60 排名本身是否打贏台指。",
            "- **L2a vs L2b**：先 ROE 再排名 vs 先排名再 ROE（FinPilot 原版為 L2b）。",
            "- **L2b 完整 s04**：重疊 L1 區間若勝率 > L1H9，代表動能+品質月頻在短持有下具參考價值。",
            "- 財報 PIT 決定 ROE 層可回測起點；`--extend-fundamentals` 可往前推。",
            "",
            "```bash",
            "PYTHONPATH=src .venv/bin/python scripts/run_s04_layer_analysis.py "
            "--extend-fundamentals --write-report",
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="s04 分層拆解")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--hold-days", type=int, default=9)
    parser.add_argument(
        "--extend-fundamentals",
        action="store_true",
        help="回補 FinMind 財報（預設回溯 2500 日 ≈ 7 年）",
    )
    parser.add_argument("--fund-lookback-days", type=int, default=2500)
    parser.add_argument("--month-start", default="2025-01", help="逐月表起始 YYYY-MM")
    parser.add_argument("--month-end", default="2026-12", help="逐月表結束 YYYY-MM")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()

    conn = connect(args.db)
    if args.extend_fundamentals:
        backfill_financial_history(conn, lookback_days=args.fund_lookback_days)

    price_range = conn.execute(
        "SELECT MIN(trade_date), MAX(trade_date) FROM stock_daily_bars"
    ).fetchone()
    fund_range = conn.execute(
        "SELECT MIN(period_date), MAX(period_date) FROM stock_financial_history "
        "WHERE period_type='quarter'"
    ).fetchone()

    l1 = _l1h9_overlap(conn)
    l1_monthly_agg, l1_monthly_detail = _l1h9_by_month(
        conn, month_start=args.month_start, month_end=args.month_end
    )
    layer_rows: list[dict] = []

    for spec in S04_LAYER_SPECS:
        print(f"回測 {spec.layer_id} {spec.label}…")
        periods = run_s04_layer_periods(
            conn, spec, hold_days=args.hold_days
        )
        summary = summarize_periods(periods)
        overlap_periods = [
            p
            for p in periods
            if l1["window_start"]
            and l1["window_end"]
            and l1["window_start"] <= p["entry_date"] <= l1["window_end"]
        ]
        layer_rows.append(
            {
                "layer_id": spec.layer_id,
                "label": spec.label,
                "periods": periods,
                "summary": summary,
                "overlap": summarize_periods(overlap_periods),
                "by_year": summarize_by_year(periods),
                "by_month": summarize_by_month(
                    periods,
                    month_start=args.month_start,
                    month_end=args.month_end,
                ),
                "roe_marginal": (
                    roe_filter_marginal_summary(periods)
                    if spec.layer_id == "L2b"
                    else {}
                ),
            }
        )

    conn.close()
    report = format_report(
        layer_rows,
        l1=l1,
        hold_days=args.hold_days,
        fund_range=fund_range,
        price_range=price_range,
        l1_monthly_agg=l1_monthly_agg,
        l1_monthly_detail=l1_monthly_detail,
        month_start=args.month_start,
        month_end=args.month_end,
    )
    print(report)
    if args.write_report:
        out = ROOT / "reports" / f"{date.today().strftime('%Y%m%d')}_s04_layer_analysis.md"
        out.write_text(report, encoding="utf-8")
        print(f"已寫入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
