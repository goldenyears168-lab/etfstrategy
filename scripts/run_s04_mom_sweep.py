#!/usr/bin/env python3
"""s04 日頻 · Mom 窗口 sweep（Top30 ± ROE>0 · H9）。"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.copytrade_backtest import compute_excess_significance  # noqa: E402
from research.backtest.finpilot_s04_layers import (  # noqa: E402
    S04_LAYER_SPECS,
    run_s04_daily_periods,
    summarize_by_month,
    summarize_periods,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

CRISIS_MONTHS = ("2025-09", "2026-05", "2026-06")
DEFAULT_MOM_DAYS = (5, 10, 20, 60)


def _parse_mom_days(raw: str) -> tuple[int, ...]:
    out = tuple(int(x.strip()) for x in raw.split(",") if x.strip())
    if not out or any(d < 1 for d in out):
        raise ValueError("mom-days must be comma-separated positive integers")
    return out


def _as_day_results(periods: list[dict]):
    from types import SimpleNamespace

    return [
        SimpleNamespace(
            status="complete",
            return_pct=p["return_pct"],
            bench_return_pct=p["bench_return_pct"],
        )
        for p in periods
    ]


def _summarize_variant(
    periods: list[dict],
    *,
    overlap_start: str,
) -> dict:
    full = summarize_periods(periods)
    sig = compute_excess_significance(_as_day_results(periods))
    overlap = [p for p in periods if p["entry_date"] >= overlap_start]
    ov_sum = summarize_periods(overlap)
    ov_sig = compute_excess_significance(_as_day_results(overlap))

    month_rows = summarize_by_month(periods, month_start="2025-01", month_end="2026-12")

    crisis: dict[str, dict] = {}
    for ym in CRISIS_MONTHS:
        sub = [r for r in month_rows if r["month"] == ym]
        if not sub:
            crisis[ym] = {"n_periods": 0}
            continue
        n = len(sub)
        crisis[ym] = {
            "n_periods": n,
            "mean_excess_pct": round(sum(r["excess_pct"] for r in sub) / n, 4),
            "win_rate_vs_bench_pct": round(
                sum(1 for r in sub if r["beat_bench"]) / n * 100, 2
            ),
        }

    return {
        "n": full["n_periods"],
        "win_rate_vs_bench_pct": full["win_rate_vs_bench_pct"],
        "win_rate_gross_pct": full["win_rate_gross_pct"],
        "mean_excess_pct": sig.get("mean_excess_pct"),
        "overlap_n": ov_sum["n_periods"],
        "overlap_wr_pct": ov_sum["win_rate_vs_bench_pct"],
        "overlap_mean_excess_pct": ov_sig.get("mean_excess_pct"),
        "crisis": crisis,
    }


def format_report(
    rows: list[dict],
    *,
    hold_days: int,
    overlap_start: str,
    mom_days: tuple[int, ...],
) -> str:
    roe_label = {True: "L2b+ROE", False: "L1c 無ROE"}
    lines = [
        "# s04 日頻 · Mom 窗口 sweep",
        "",
        f"> 訊號：每交易日 · MomN Top30 · T+1 開盤 · H{hold_days} 收盤",
        f"> 基準 IX0001 · L1 重疊區 entry ≥ {overlap_start}",
        f"> sweep：Mom {', '.join(str(d) for d in mom_days)} × ROE 有/無",
        "",
        "## 全樣本",
        "",
        "| Mom | 規格 | n | 勝台指% | 勝率（毛） | 均超額% |",
        "|-----|------|---|---------|-----------|---------|",
    ]
    for r in rows:
        lines.append(
            f"| Mom{r['mom_lookback']} | {roe_label[r['with_roe']]} | {r['n']} | "
            f"{r['win_rate_vs_bench_pct']}% | {r['win_rate_gross_pct']}% | "
            f"{r['mean_excess_pct']} |"
        )

    lines.extend(
        [
            "",
            f"## L1 重疊區（entry ≥ {overlap_start}）",
            "",
            "| Mom | 規格 | n | 勝台指% | 均超額% |",
            "|-----|------|---|---------|---------|",
        ]
    )
    for r in rows:
        lines.append(
            f"| Mom{r['mom_lookback']} | {roe_label[r['with_roe']]} | {r['overlap_n']} | "
            f"{r['overlap_wr_pct']}% | {r['overlap_mean_excess_pct']} |"
        )

    lines.extend(
        [
            "",
            "## 虧損月（進場月均值超額 · 日頻聚合）",
            "",
            "| Mom | 規格 | 2025-09 超額% | 勝台指% | 2026-05 超額% | 勝台指% | 2026-06 超額% | 勝台指% |",
            "|-----|------|---------------|---------|---------------|---------|---------------|---------|",
        ]
    )
    for r in rows:
        c = r["crisis"]
        lines.append(
            f"| Mom{r['mom_lookback']} | {roe_label[r['with_roe']]} | "
            f"{c['2025-09'].get('mean_excess_pct', '—')} | {c['2025-09'].get('win_rate_vs_bench_pct', '—')}% | "
            f"{c['2026-05'].get('mean_excess_pct', '—')} | {c['2026-05'].get('win_rate_vs_bench_pct', '—')}% | "
            f"{c['2026-06'].get('mean_excess_pct', '—')} | {c['2026-06'].get('win_rate_vs_bench_pct', '—')}% |"
        )

    best = max(rows, key=lambda x: (x["win_rate_vs_bench_pct"], x["mean_excess_pct"] or -999))
    lines.extend(
        [
            "",
            "## 解讀",
            "",
            f"- **全樣本最佳勝台指**：Mom{best['mom_lookback']} {roe_label[best['with_roe']]} "
            f"（{best['win_rate_vs_bench_pct']}% · 均超額 {best['mean_excess_pct']}%）。",
            "- 日頻 H9 窗重疊，月聚合與全樣本 p 值應保守解讀。",
            "- 未扣交易成本；短 Mom + 日頻 turnover 更高。",
            "",
            "```bash",
            "PYTHONPATH=src .venv/bin/python scripts/run_s04_mom_sweep.py --write-report",
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="s04 Mom lookback sweep (daily)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--hold-days", type=int, default=9)
    parser.add_argument("--mom-days", default=",".join(str(d) for d in DEFAULT_MOM_DAYS))
    parser.add_argument("--overlap-start", default="2025-05-28")
    parser.add_argument("--skip-no-roe", action="store_true", help="只跑 L2b（有 ROE）")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()

    mom_days = _parse_mom_days(args.mom_days)
    spec_roe = next(s for s in S04_LAYER_SPECS if s.layer_id == "L2b")
    spec_no_roe = next(s for s in S04_LAYER_SPECS if s.layer_id == "L1c")

    conn = connect(args.db)
    rows: list[dict] = []

    for mom in mom_days:
        variants = [(True, spec_roe)]
        if not args.skip_no_roe:
            variants.append((False, spec_no_roe))

        for with_roe, spec in variants:
            label = f"Mom{mom} {'L2b' if with_roe else 'L1c'}"
            print(f"日頻 {label}…", flush=True)
            periods = run_s04_daily_periods(
                conn,
                spec,
                hold_days=args.hold_days,
                mom_lookback=mom,
            )
            print(f"  n={len(periods)}", flush=True)
            summary = _summarize_variant(
                periods, overlap_start=args.overlap_start
            )
            rows.append(
                {
                    "mom_lookback": mom,
                    "with_roe": with_roe,
                    **summary,
                }
            )

    conn.close()

    report = format_report(
        rows,
        hold_days=args.hold_days,
        overlap_start=args.overlap_start,
        mom_days=mom_days,
    )
    print(report)

    if args.write_report:
        out = ROOT / "reports" / f"{date.today().strftime('%Y%m%d')}_s04_mom_sweep_daily.md"
        out.write_text(report, encoding="utf-8")
        print(f"已寫入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
