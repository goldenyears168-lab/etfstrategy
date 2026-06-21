#!/usr/bin/env python3
"""
文献期反向工程：dense 逐日扫描 + 参数网格校准，目标文献重叠 ≥80%。

步骤：
  1. （可选）sync 美股 K 线 → stocks.db us_daily_bars
  2. 对每个金标准案例的 literature_start~end 逐日 evaluate
  3. 网格搜索 US 专用参数（Trend / T1 深度 / 收斂比）
  4. 输出 config/vcp_us_calibrated.yaml + reports/vcp_us_literature_audit.md

用法：
  PYTHONPATH=src python src/vcp_us_literature_audit.py --sync-db
  PYTHONPATH=src python src/vcp_us_literature_audit.py --target-overlap 0.8
"""

from __future__ import annotations

import argparse
import itertools
import sys
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml

from finmind_client import finmind_token
from stock_db import DEFAULT_DB_PATH, PROJECT_ROOT, connect
from report_paths import REPORTS_RESEARCH
from vcp_nse_port.evaluate import evaluate_vcp_nse, evaluate_vcp_nse_diagnostic
from .vcp_us_cases import DEFAULT_CASES_PATH, VcpUsCase, load_vcp_us_cases
from .vcp_us_data import load_us_panel, sync_us_ticker

DEFAULT_OUTPUT = REPORTS_RESEARCH / "vcp_us_literature_audit.md"
DEFAULT_CALIBRATION = PROJECT_ROOT / "config" / "vcp_us_calibrated.yaml"

US_PARAM_GRID: dict[str, tuple] = {
    "trend_min_score": (71.0, 77.0, 85.0),
    "lookback_days": (90, 120),
    "t1_depth_max": (40.0, 50.0, 60.0),
    "contraction_ratio": (0.75, 0.85),
    "min_contractions": (2,),
}


@dataclass(frozen=True)
class VcpUsParams:
    trend_min_score: float = 85.0
    lookback_days: int = 120
    min_contractions: int = 2
    t1_depth_min: float = 10.0
    t1_depth_max: float = 40.0
    contraction_ratio: float = 0.75

    def as_kwargs(self) -> dict:
        return asdict(self)


def _pad_case_dates(case: VcpUsCase, pad_days: int) -> tuple[date, date]:
    if pad_days <= 0:
        return case.literature_start, case.literature_end
    return (
        case.literature_start - timedelta(days=pad_days),
        case.literature_end + timedelta(days=pad_days),
    )


def dense_scan_case(
    case: VcpUsCase,
    stock_df: pd.DataFrame,
    bench_df: pd.DataFrame,
    params: VcpUsParams,
    *,
    min_composite: float,
    pad_days: int = 0,
    diagnostic: bool = False,
) -> dict:
    """Scan every trading day in literature window (+ optional pad)."""
    lit_start, lit_end = _pad_case_dates(case, pad_days)
    hits: list[dict] = []
    reject_stages: dict[str, int] = {"trend": 0, "vcp": 0, "bars": 0}
    best_passed_score = 0.0
    best_passed_date: str | None = None
    max_trend = 0.0
    vcp_ok_days = 0

    dates = stock_df["date"]
    for idx in range(200, len(stock_df)):
        as_of = dates.iloc[idx].date()
        if as_of < lit_start or as_of > lit_end:
            continue

        stock_slice = stock_df.iloc[: idx + 1].copy()
        bench_slice = bench_df[bench_df["date"] <= dates.iloc[idx]]
        if len(bench_slice) < 200:
            continue

        if diagnostic:
            diag = evaluate_vcp_nse_diagnostic(
                stock_slice, bench_slice, **params.as_kwargs()
            )
            max_trend = max(max_trend, float(diag["trend_score"]))
            if diag["vcp_ok"]:
                vcp_ok_days += 1
            st = str(diag.get("reject_stage") or "")
            if st in reject_stages:
                reject_stages[st] += 1

        result = evaluate_vcp_nse(stock_slice, bench_slice, **params.as_kwargs())
        if not result.get("passed"):
            continue
        score = float(result["composite_score"])
        if score >= min_composite:
            hits.append({"as_of": str(as_of), "composite_score": score})
        if score > best_passed_score:
            best_passed_score = score
            best_passed_date = str(as_of)

    return {
        "case_id": case.case_id,
        "hits": hits,
        "hit_count": len(hits),
        "overlap": len(hits) > 0,
        "best_passed_score": best_passed_score,
        "best_passed_date": best_passed_date,
        "max_trend_in_window": round(max_trend, 1),
        "vcp_ok_days": vcp_ok_days,
        "reject_stages": reject_stages,
    }


def count_overlaps(
    cases: tuple[VcpUsCase, ...],
    panels: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
    params: VcpUsParams,
    *,
    min_composite: float,
    pad_days: int,
) -> tuple[int, list[dict]]:
    rows: list[dict] = []
    overlap_n = 0
    for case in cases:
        stock_df = panels.get(case.ticker)
        if stock_df is None:
            rows.append(
                {
                    "case_id": case.case_id,
                    "overlap": False,
                    "hit_count": 0,
                    "note": "no data",
                }
            )
            continue
        row = dense_scan_case(
            case,
            stock_df,
            bench_df,
            params,
            min_composite=min_composite,
            pad_days=pad_days,
            diagnostic=True,
        )
        if row["overlap"]:
            overlap_n += 1
        rows.append(row)
    return overlap_n, rows


def search_calibration(
    cases: tuple[VcpUsCase, ...],
    panels: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
    *,
    min_composite: float,
    target_overlap: float,
) -> tuple[VcpUsParams | None, list[dict], int, int]:
    keys = list(US_PARAM_GRID.keys())
    best_params: VcpUsParams | None = None
    best_rows: list[dict] = []
    best_overlap = -1
    best_pad = 0

    for pad_days in (0, 10, 21):
        for combo in itertools.product(*(US_PARAM_GRID[k] for k in keys)):
            params = VcpUsParams(**dict(zip(keys, combo)))
            overlap_n, rows = count_overlaps(
                cases,
                panels,
                bench_df,
                params,
                min_composite=min_composite,
                pad_days=pad_days,
            )
            if overlap_n > best_overlap:
                best_overlap = overlap_n
                best_params = params
                best_rows = rows
                best_pad = pad_days
            if overlap_n >= int(len(cases) * target_overlap):
                return params, rows, overlap_n, pad_days

    return best_params, best_rows, best_overlap, best_pad


def write_calibration_yaml(
    path: Path,
    params: VcpUsParams,
    *,
    min_composite: float,
    pad_days: int,
    overlap_n: int,
    n_cases: int,
) -> None:
    payload = {
        "description": (
            "US VCP 文献反向工程校准参数（勿直接用于台股 vcp_screen；"
            "台股请维持 vcp-nse-port 默认或另行校准 IX0001）"
        ),
        "model_id": "vcp-nse-port-us-calibrated",
        "min_composite": min_composite,
        "literature_pad_days": pad_days,
        "calibration_overlap": f"{overlap_n}/{n_cases}",
        "params": params.as_kwargs(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def build_audit_markdown(
    *,
    cases_path: Path,
    params_default: VcpUsParams,
    rows_default: list[dict],
    overlap_default: int,
    params_cal: VcpUsParams | None,
    rows_cal: list[dict],
    overlap_cal: int,
    min_composite: float,
    pad_days: int,
    n_cases: int,
    data_source: str,
) -> str:
    lines = [
        "# VCP 文献反向工程 audit",
        "",
        f"- Cases: `{cases_path.relative_to(PROJECT_ROOT)}`",
        f"- Data: {data_source}",
        f"- Target: composite ≥ **{min_composite}** · overlap ≥ **80%**",
        "",
        "## 为何 walk-forward 重叠低？",
        "",
        "1. **取样稀疏**：每 5 日 scan 会错过短文献窗",
        "2. **参数偏 NSE/印度**：Trend≥85（6/7）+ T1≤40% 对美股波动偏严",
        "3. **文献期是 eyeball 标注**：与算法 pivot 日不一定同一天",
        "",
        "## 默认参数 · 文献 dense scan",
        "",
        f"Params: `{params_default.as_kwargs()}` · pad={pad_days}d",
        f"**重叠：{overlap_default}/{n_cases}** ({overlap_default / n_cases:.0%})",
        "",
        "| case | hits | best | max trend | vcp_ok days | 主拒绝 |",
        "|------|------|------|-----------|-------------|--------|",
    ]

    for row in rows_default:
        if row.get("note") == "no data":
            lines.append(f"| {row['case_id']} | — | — | — | — | 无 K 线 |")
            continue
        rs = row.get("reject_stages") or {}
        top_reject = max(rs, key=lambda k: rs[k]) if rs else "—"
        lines.append(
            f"| {row['case_id']} | {row['hit_count']} | "
            f"{row.get('best_passed_score') or '—'} | "
            f"{row.get('max_trend_in_window', '—')} | "
            f"{row.get('vcp_ok_days', 0)} | {top_reject} |"
        )

    lines.extend(["", "## 网格校准后", ""])
    if params_cal:
        lines.append(f"Params: `{params_cal.as_kwargs()}`")
        lines.append(f"**重叠：{overlap_cal}/{n_cases}** ({overlap_cal / n_cases:.0%})")
        lines.extend(["", "| case | hits | overlap | best date |", "|------|------|---------|-----------|"])
        for row in rows_cal:
            ov = "✓" if row.get("overlap") else "—"
            lines.append(
                f"| {row['case_id']} | {row.get('hit_count', 0)} | {ov} | "
                f"{row.get('best_passed_date') or '—'} |"
            )
    else:
        lines.append("_校准未找到更佳参数组合。_")

    lines.extend(
        [
            "",
            "## 如何使用校准结果",
            "",
            "1. **验证用**：benchmark 加 `--calibrated config/vcp_us_calibrated.yaml`",
            "2. **台股**：仅当 IX0001 上 P@K 亦改善才考虑移植 params",
            "3. **FinMind DB**：`--sync-db` 缓存 us_daily_bars，可修复 WISH 等 yfinance 缺档",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def sync_cases_to_db(
    conn,
    config,
    start: date,
    end: date,
    *,
    prefer_finmind: bool,
) -> None:
    tickers = config.tickers + (config.benchmark,)
    seen: set[str] = set()
    for t in tickers:
        if t in seen:
            continue
        seen.add(t)
        try:
            n, src = sync_us_ticker(conn, t, start, end, prefer_finmind=prefer_finmind)
            print(f"  sync {t}: {n} bars ({src})")
        except Exception as exc:
            print(f"  WARN sync {t}: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="VCP 文献反向工程 audit")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--sync-db", action="store_true", help="先 sync → us_daily_bars")
    parser.add_argument("--use-db", action="store_true", help="读/写 us_daily_bars 缓存")
    parser.add_argument("--yfinance-only", action="store_true")
    parser.add_argument("--min-composite", type=float, default=65.0)
    parser.add_argument("--target-overlap", type=float, default=0.8)
    parser.add_argument("--literature-pad-days", type=int, default=0)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--calibration-out", type=Path, default=DEFAULT_CALIBRATION)
    args = parser.parse_args()

    config = load_vcp_us_cases(args.cases)
    end = date.today()
    start = config.fetch_start(end)

    prefer_finmind = not args.yfinance_only
    if prefer_finmind and not finmind_token():
        print("NOTE: 无 FINMIND_TOKEN，用 yfinance", file=sys.stderr)
        prefer_finmind = False

    conn = connect(args.db) if (args.sync_db or args.use_db) else None
    try:
        if args.sync_db and conn:
            print(f"Sync US bars → {args.db} …")
            sync_cases_to_db(conn, config, start, end, prefer_finmind=prefer_finmind)

        panels, bench_df, source = load_us_panel(
            config.tickers,
            config.benchmark,
            start,
            end,
            conn=conn,
            use_db=args.use_db or args.sync_db,
            prefer_finmind=prefer_finmind,
        )
    finally:
        if conn:
            conn.close()

    if not panels or bench_df.empty:
        print("ERROR: 无 OHLCV", file=sys.stderr)
        return 1

    default_params = VcpUsParams()
    overlap_def, rows_def = count_overlaps(
        config.cases,
        panels,
        bench_df,
        default_params,
        min_composite=args.min_composite,
        pad_days=args.literature_pad_days,
    )

    params_cal, rows_cal, overlap_cal, pad_cal = search_calibration(
        config.cases,
        panels,
        bench_df,
        min_composite=args.min_composite,
        target_overlap=args.target_overlap,
    )

    n = len(config.cases)
    md = build_audit_markdown(
        cases_path=args.cases,
        params_default=default_params,
        rows_default=rows_def,
        overlap_default=overlap_def,
        params_cal=params_cal,
        rows_cal=rows_cal,
        overlap_cal=overlap_cal,
        min_composite=args.min_composite,
        pad_days=pad_cal,
        n_cases=n,
        data_source=source,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(md, encoding="utf-8")

    if params_cal:
        write_calibration_yaml(
            args.calibration_out,
            params_cal,
            min_composite=args.min_composite,
            pad_days=pad_cal,
            overlap_n=overlap_cal,
            n_cases=n,
        )

    print(f"  default dense overlap: {overlap_def}/{n}")
    print(f"  calibrated overlap:    {overlap_cal}/{n} (pad={pad_cal}d)")
    if params_cal:
        print(f"  calibration → {args.calibration_out.relative_to(PROJECT_ROOT)}")
    print(f"  audit → {args.output.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
