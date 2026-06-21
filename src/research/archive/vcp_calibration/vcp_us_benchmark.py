#!/usr/bin/env python3
"""
美股 VCP 金标准 walk-forward（config/vcp_us_cases.yaml）。

对照 vcp-nse-port composite ≥65 / ≥80 之前瞻 20d/60d 表现，
并输出每档文献案例的命中次数 vs 文献时期重叠。

用法：
  PYTHONPATH=src python src/vcp_us_benchmark.py
  PYTHONPATH=src python src/vcp_us_benchmark.py --cases config/vcp_us_cases.yaml
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml

from finmind_client import finmind_token
from stock_db import DEFAULT_DB_PATH, PROJECT_ROOT, connect
from report_paths import REPORTS_RESEARCH
from vcp_nse_port.evaluate import evaluate_vcp_nse
from .vcp_us_cases import (
    DEFAULT_CASES_PATH,
    load_vcp_us_cases,
    summarize_case_hits,
)
from .vcp_us_data import load_us_panel
from .vcp_us_literature_audit import VcpUsParams, dense_scan_case

DEFAULT_OUTPUT = REPORTS_RESEARCH / "vcp_us_benchmark.md"
DEFAULT_CALIBRATION = PROJECT_ROOT / "config" / "vcp_us_calibrated.yaml"
SOURCE_ATTRIBUTION = (
    "Scoring: github.com/ajeeshworkspace/indian-trading-skills/nse-vcp-screener · "
    "Cases: config/vcp_us_cases.yaml"
)


def _load_calibrated_params(path: Path) -> tuple[VcpUsParams, float, int]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    p = raw.get("params") or {}
    return (
        VcpUsParams(**p),
        float(raw.get("min_composite") or 65),
        int(raw.get("literature_pad_days") or 0),
    )


def run_dense_literature_signals(
    config,
    panels: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
    params: VcpUsParams,
    *,
    min_composite: float,
    pad_days: int,
    forward_horizons: tuple[int, ...],
) -> list[dict]:
    """Literature-window dense scan (reverse-engineering mode)."""
    signals: list[dict] = []
    for case in config.cases:
        stock_df = panels.get(case.ticker)
        if stock_df is None:
            continue
        row = dense_scan_case(
            case,
            stock_df,
            bench_df,
            params,
            min_composite=min_composite,
            pad_days=pad_days,
        )
        closes = stock_df["Close"]
        dates = stock_df["date"]
        date_to_idx = {dates.iloc[i].date(): i for i in range(len(stock_df))}
        for hit in row["hits"]:
            as_of = date.fromisoformat(hit["as_of"])
            idx = date_to_idx.get(as_of)
            if idx is None:
                continue
            sig = {
                "ticker": case.ticker,
                "case_id": case.case_id,
                "as_of": hit["as_of"],
                "composite_score": hit["composite_score"],
            }
            for h in forward_horizons:
                if idx + h < len(closes):
                    base = float(closes.iloc[idx])
                    future = float(closes.iloc[idx + h])
                    if base > 0:
                        sig[f"fwd_{h}d_pct"] = (future - base) / base * 100.0
            signals.append(sig)
    return signals


def _forward_return(closes: pd.Series, idx: int, horizon: int) -> float | None:
    if idx + horizon >= len(closes):
        return None
    base = float(closes.iloc[idx])
    future = float(closes.iloc[idx + horizon])
    if base <= 0:
        return None
    return (future - base) / base * 100.0


def run_walkforward(
    panels: dict[str, pd.DataFrame],
    bench_full: pd.DataFrame,
    *,
    sample_every: int,
    forward_horizons: tuple[int, ...],
    eval_params: VcpUsParams | None = None,
) -> list[dict]:
    """Collect all vcp-nse-port passed signals (any composite score)."""
    kwargs = eval_params.as_kwargs() if eval_params else {}
    signals: list[dict] = []
    max_h = max(forward_horizons)

    for ticker, full_df in panels.items():
        closes = full_df["Close"]
        dates = full_df["date"]
        start_idx = 200
        for idx in range(start_idx, len(full_df) - max_h - 1, sample_every):
            stock_slice = full_df.iloc[: idx + 1].copy()
            as_of = dates.iloc[idx]
            bench_slice = bench_full[bench_full["date"] <= as_of]
            if len(bench_slice) < 200:
                continue

            result = evaluate_vcp_nse(stock_slice, bench_slice, **kwargs)
            if not result.get("passed"):
                continue

            row: dict = {
                "ticker": ticker,
                "as_of": str(as_of.date()),
                "composite_score": float(result["composite_score"]),
                "quality": result.get("quality"),
            }
            for h in forward_horizons:
                row[f"fwd_{h}d_pct"] = _forward_return(closes, idx, h)
            signals.append(row)

    return signals


def _filter_signals(signals: list[dict], min_score: float) -> list[dict]:
    return [s for s in signals if float(s["composite_score"]) >= min_score]


def _aggregate_forward_stats(
    signals: list[dict],
    forward_horizons: tuple[int, ...],
) -> list[str]:
    if not signals:
        return ["_No signals at this threshold._"]
    lines = [
        "| Horizon | Mean % | Median % | Hit rate >0% | N |",
        "|---------|--------|----------|--------------|---|",
    ]
    for h in forward_horizons:
        key = f"fwd_{h}d_pct"
        vals = [s[key] for s in signals if s.get(key) is not None]
        if not vals:
            continue
        hit = sum(1 for v in vals if v > 0) / len(vals) * 100
        lines.append(
            f"| {h}d | {statistics.mean(vals):+.2f} | {statistics.median(vals):+.2f} | {hit:.0f}% | {len(vals)} |"
        )
    return lines


def build_report_markdown(
    *,
    config_path: Path,
    data_source: str,
    fetch_start: date,
    fetch_end: date,
    panels_loaded: int,
    all_signals: list[dict],
    case_rows: list[dict],
    score_thresholds: tuple[float, ...],
    forward_horizons: tuple[int, ...],
    model_id: str,
    sample_every: int,
) -> str:
    lines = [
        f"# VCP US benchmark · {SOURCE_ATTRIBUTION}",
        "",
        f"- Config: `{config_path.relative_to(PROJECT_ROOT)}`",
        f"- Model: `{model_id}`",
        f"- Data: **{data_source}** · fetch `{fetch_start}` ~ `{fetch_end}`",
        f"- Universe loaded: **{panels_loaded}** tickers (≥200 bars)",
        f"- Raw passed signals (all scores): **{len(all_signals)}**",
        "",
    ]

    for thr in score_thresholds:
        thr_key = int(thr) if thr == int(thr) else thr
        subset = _filter_signals(all_signals, thr)
        lines.extend(
            [
                f"## Aggregate · composite ≥ {thr_key} (n={len(subset)})",
                "",
                *_aggregate_forward_stats(subset, forward_horizons),
                "",
            ]
        )

    thr_cols = score_thresholds
    header = (
        "| case | ticker | 文献期 | "
        + " | ".join(f"≥{int(t) if t == int(t) else t} 全/文献/重叠" for t in thr_cols)
        + " | 来源 |"
    )
    sep = (
        "|------|--------|--------|"
        + "|".join("------------------:" for _ in thr_cols)
        + "------|"
    )
    lines.extend(["## 金标准 20 例 · 命中 vs 文献时期", "", header, sep])

    overlap_counts = {t: 0 for t in score_thresholds}
    for row in case_rows:
        cells: list[str] = []
        for thr in thr_cols:
            thr_key = int(thr) if thr == int(thr) else thr
            total = row[f"hits_{thr_key}_total"]
            in_lit = row[f"hits_{thr_key}_in_literature"]
            overlap = "✓" if row[f"overlap_{thr_key}"] else "—"
            if row[f"overlap_{thr_key}"]:
                overlap_counts[thr] += 1
            cells.append(f"{total}/{in_lit}/{overlap}")
        lines.append(
            f"| {row['case_id']} | {row['ticker']} | {row['literature']} | "
            + " | ".join(cells)
            + f" | {row['source']} |"
        )

    lines.extend(["", "### 文献重叠摘要", ""])
    n_cases = len(case_rows)
    for thr in score_thresholds:
        thr_key = int(thr) if thr == int(thr) else thr
        lines.append(
            f"- composite ≥ **{thr_key}**：{overlap_counts[thr]}/{n_cases} 案例在文献窗口内至少 1 次命中"
        )

    lines.extend(["", "## 说明", ""])
    lines.append(
        "表格「全/文献/重叠」= walk-forward 命中次数（全样本）/ 文献期内命中 / 是否与文献期重叠（✓）。"
    )
    lines.append(
        f"同一 ticker 多案例（如 TSLA×3）分别计；walk-forward 每 **{sample_every}** 交易日取样。"
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="美股 VCP 金标准 walk-forward")
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_PATH,
        help="金标准案例 YAML（默认 config/vcp_us_cases.yaml）",
    )
    parser.add_argument(
        "--tickers",
        default=None,
        help="覆盖 YAML universe（逗号分隔；默认从 cases 推导）",
    )
    parser.add_argument("--benchmark", default=None, help="覆盖 YAML benchmark")
    parser.add_argument(
        "--years",
        type=int,
        default=None,
        help="回溯年数（默认依 cases 最早文献期自动拉长）",
    )
    parser.add_argument(
        "--min-scores",
        default=None,
        help="覆盖 YAML score_thresholds，如 65,80",
    )
    parser.add_argument("--sample-every", type=int, default=None)
    parser.add_argument("--forward-days", default=None, help="覆盖 YAML forward_days")
    parser.add_argument("--yfinance-only", action="store_true")
    parser.add_argument("--use-db", action="store_true", help="读 us_daily_bars 缓存")
    parser.add_argument(
        "--dense-literature",
        action="store_true",
        help="文献期逐日 dense scan（非 walk-forward 稀疏取样）",
    )
    parser.add_argument(
        "--calibrated",
        type=Path,
        default=None,
        help="使用 vcp_us_calibrated.yaml 参数（先跑 literature_audit）",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    if not args.cases.is_file():
        print(f"ERROR: cases file not found: {args.cases}", file=sys.stderr)
        return 1

    config = load_vcp_us_cases(args.cases)
    benchmark = (args.benchmark or config.benchmark).upper()
    tickers = (
        tuple(t.strip().upper() for t in args.tickers.split(",") if t.strip())
        if args.tickers
        else config.tickers
    )
    score_thresholds = (
        tuple(float(x.strip()) for x in args.min_scores.split(",") if x.strip())
        if args.min_scores
        else config.score_thresholds
    )
    forward_horizons = (
        tuple(int(x.strip()) for x in args.forward_days.split(",") if x.strip())
        if args.forward_days
        else config.forward_days
    )
    sample_every = args.sample_every or config.sample_every

    eval_params: VcpUsParams | None = None
    lit_pad = 0
    min_composite_lit = min(score_thresholds) if score_thresholds else 65.0
    cal_path = (args.calibrated or DEFAULT_CALIBRATION).resolve()
    if cal_path.is_file():
        eval_params, min_composite_lit, lit_pad = _load_calibrated_params(cal_path)
        print(f"Using calibration: {cal_path.relative_to(PROJECT_ROOT.resolve())}", file=sys.stderr)

    prefer_finmind = not args.yfinance_only
    if prefer_finmind and not finmind_token():
        print("NOTE: 無 FINMIND_TOKEN，改用 yfinance", file=sys.stderr)
        prefer_finmind = False

    end = date.today()
    if args.years is not None:
        start = end - timedelta(days=args.years * 366)
    else:
        start = config.fetch_start(end)

    print(
        f"Fetching {len(tickers)} tickers + {benchmark} "
        f"({start} ~ {end}) …"
    )
    conn = connect(args.db) if args.use_db else None
    try:
        panels, bench_df, data_source = load_us_panel(
            tickers,
            benchmark,
            start,
            end,
            conn=conn,
            use_db=args.use_db,
            prefer_finmind=prefer_finmind,
        )
    finally:
        if conn:
            conn.close()
    print(f"  loaded {len(panels)}/{len(tickers)} symbols")

    if not panels or bench_df.empty:
        print("ERROR: 無 OHLCV（确认网络 / pip install yfinance）", file=sys.stderr)
        return 1

    model_id = config.model_id
    if eval_params:
        model_id = "vcp-nse-port-us-calibrated"

    if args.dense_literature:
        params = eval_params or VcpUsParams()
        all_signals = run_dense_literature_signals(
            config,
            panels,
            bench_df,
            params,
            min_composite=min_composite_lit,
            pad_days=lit_pad,
            forward_horizons=forward_horizons,
        )
        from .vcp_us_literature_audit import count_overlaps

        _, scan_rows = count_overlaps(
            config.cases,
            panels,
            bench_df,
            params,
            min_composite=min_composite_lit,
            pad_days=lit_pad,
        )
        case_rows = []
        for sr in scan_rows:
            case_rows.append(
                {
                    "case_id": sr["case_id"],
                    "ticker": next(
                        c.ticker for c in config.cases if c.case_id == sr["case_id"]
                    ),
                    "literature": next(
                        f"{c.literature_start} ~ {c.literature_end}"
                        for c in config.cases
                        if c.case_id == sr["case_id"]
                    ),
                    "source": next(
                        c.source for c in config.cases if c.case_id == sr["case_id"]
                    ),
                    **{
                        f"hits_{int(t) if t == int(t) else t}_total": sr.get(
                            "hit_count", 0
                        )
                        for t in score_thresholds
                    },
                    **{
                        f"hits_{int(t) if t == int(t) else t}_in_literature": sr.get(
                            "hit_count", 0
                        )
                        if float(t) <= min_composite_lit
                        else 0
                        for t in score_thresholds
                    },
                    **{
                        f"overlap_{int(t) if t == int(t) else t}": sr.get("overlap")
                        and float(t) <= min_composite_lit
                        for t in score_thresholds
                    },
                }
            )
        sample_note = f"dense literature (pad={lit_pad}d)"
    else:
        all_signals = run_walkforward(
            panels,
            bench_df,
            sample_every=sample_every,
            forward_horizons=forward_horizons,
            eval_params=eval_params,
        )
        case_rows = summarize_case_hits(
            all_signals, config, score_thresholds=score_thresholds
        )
        sample_note = str(sample_every)

    md = build_report_markdown(
        config_path=args.cases,
        data_source=data_source,
        fetch_start=start,
        fetch_end=end,
        panels_loaded=len(panels),
        all_signals=all_signals,
        case_rows=case_rows,
        score_thresholds=score_thresholds,
        forward_horizons=forward_horizons,
        model_id=model_id,
        sample_every=sample_every if not args.dense_literature else 1,
    )
    if args.dense_literature:
        md = md.replace(
            f"walk-forward 每 **{sample_every}** 交易日取样",
            f"文献期 **dense** 逐日扫描（{sample_note}）",
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(md, encoding="utf-8")

    for thr in score_thresholds:
        n = len(_filter_signals(all_signals, thr))
        lit = sum(1 for r in case_rows if r[f"overlap_{int(thr) if thr == int(thr) else thr}"])
        print(f"  ≥{thr}: signals={n} · literature overlap cases={lit}/{len(case_rows)}")
    print(f"  → {args.output.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
