#!/usr/bin/env python3
"""
台股 VCP 金标准 walk-forward（config/vcp_tw_cases.yaml）。

对照 vcp-tm / vcp-nse-port composite ≥65 / ≥80 之前瞻 20d/60d 表现，
并输出每档文献案例的命中次数 vs 文献时期重叠。

用法：
  PYTHONPATH=src python src/vcp_tw_benchmark.py --use-db
  PYTHONPATH=src python src/vcp_tw_benchmark.py --use-db --compare-legacy \\
    --calibrated config/vcp_tm_calibrated.yaml
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import fields
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml

from finmind_client import finmind_token
from report_paths import REPORTS_RESEARCH
from stock_db import DEFAULT_DB_PATH, PROJECT_ROOT, connect
from vcp_nse_port.evaluate import evaluate_vcp_nse
from vcp_tm.evaluate import evaluate_vcp_tm
from vcp_tm.params import VcpTmParams
from .vcp_tw_cases import (
    DEFAULT_CASES_PATH,
    load_vcp_tw_cases,
    summarize_case_hits,
)
from .vcp_tw_data import load_tw_panel
from .vcp_tw_literature_audit import VcpTwParams, dense_scan_case

DEFAULT_OUTPUT = REPORTS_RESEARCH / "vcp_tw_benchmark.md"
DEFAULT_CALIBRATION = PROJECT_ROOT / "config" / "vcp_tm_calibrated.yaml"
SOURCE_ATTRIBUTION = (
    "Scoring: tradermonty/claude-trading-skills (VCP-TM) · "
    "Cases: config/vcp_tw_cases.yaml · Benchmark: IX0001"
)


def _load_calibrated_params(path: Path) -> tuple[VcpTmParams, float, int]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    p = raw.get("params") or {}
    known = {f.name for f in fields(VcpTmParams)}
    filtered = {k: v for k, v in p.items() if k in known}
    return (
        VcpTmParams(**filtered),
        float(raw.get("min_composite") or 65),
        int(raw.get("literature_pad_days") or 0),
    )



def run_dense_literature_signals(
    config,
    panels: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
    params: VcpTmParams,
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
    eval_params: VcpTmParams | None = None,
    engine: str = "vcp-tm",
) -> list[dict]:
    """Collect passed signals (walk-forward). engine: vcp-tm | vcp-nse-port."""
    tm_params = eval_params or VcpTmParams()
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

            if engine == "vcp-nse-port":
                legacy_kw = {
                    k: v
                    for k, v in tm_params.as_kwargs().items()
                    if k
                    in (
                        "trend_min_score",
                        "lookback_days",
                        "min_contractions",
                        "t1_depth_min",
                        "t1_depth_max",
                        "contraction_ratio",
                    )
                }
                result = evaluate_vcp_nse(stock_slice, bench_slice, **legacy_kw)
            else:
                result = evaluate_vcp_tm(stock_slice, bench_slice, params=tm_params)
            if not result.get("passed"):
                continue

            row: dict = {
                "ticker": ticker,
                "as_of": str(as_of.date()),
                "composite_score": float(result["composite_score"]),
                "quality": result.get("rating") or result.get("quality"),
            }
            for h in forward_horizons:
                row[f"fwd_{h}d_pct"] = _forward_return(closes, idx, h)
            signals.append(row)

    return signals


def discover_historical_cases(
    config,
    panels: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
    params: VcpTmParams,
    *,
    min_composite: float = 65.0,
    sample_every: int = 5,
    top_n: int = 5,
    forward_horizons: tuple[int, ...] = (20, 60),
    engine: str = "vcp-tm",
) -> list[dict]:
    """Find high-score VCP signals outside literature windows (data-driven cases)."""
    lit_windows: dict[str, list[tuple[date, date]]] = {}
    for case in config.cases:
        lit_windows.setdefault(case.ticker, []).append(
            (case.literature_start, case.literature_end)
        )

    def in_any_lit_window(ticker: str, as_of: date) -> bool:
        for start, end in lit_windows.get(ticker, []):
            if start <= as_of <= end:
                return True
        return False

    candidates: list[dict] = []
    max_h = max(forward_horizons)
    legacy_kw = {
        k: v
        for k, v in params.as_kwargs().items()
        if k
        in (
            "trend_min_score",
            "lookback_days",
            "min_contractions",
            "t1_depth_min",
            "t1_depth_max",
            "contraction_ratio",
        )
    }

    for ticker, full_df in panels.items():
        closes = full_df["Close"]
        dates = full_df["date"]
        for idx in range(200, len(full_df) - max_h - 1, sample_every):
            as_of = dates.iloc[idx].date()
            if in_any_lit_window(ticker, as_of):
                continue
            stock_slice = full_df.iloc[: idx + 1].copy()
            bench_slice = bench_df[bench_df["date"] <= dates.iloc[idx]]
            if len(bench_slice) < 200:
                continue
            if engine == "vcp-nse-port":
                result = evaluate_vcp_nse(stock_slice, bench_slice, **legacy_kw)
            else:
                result = evaluate_vcp_tm(stock_slice, bench_slice, params=params)
            if not result.get("passed"):
                continue
            score = float(result["composite_score"])
            if score < min_composite:
                continue
            row: dict = {
                "ticker": ticker,
                "as_of": str(as_of),
                "composite_score": score,
                "quality": result.get("rating") or result.get("quality"),
            }
            for h in forward_horizons:
                row[f"fwd_{h}d_pct"] = _forward_return(closes, idx, h)
            candidates.append(row)

    candidates.sort(
        key=lambda r: (
            float(r["composite_score"]),
            float(r.get("fwd_20d_pct") or -999),
        ),
        reverse=True,
    )
    seen: set[tuple[str, str]] = set()
    picked: list[dict] = []
    for row in candidates:
        key = (row["ticker"], row["as_of"][:7])
        if key in seen:
            continue
        seen.add(key)
        picked.append(row)
        if len(picked) >= top_n:
            break
    return picked


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
    discovered: list[dict] | None = None,
) -> str:
    lines = [
        f"# VCP TW benchmark · {SOURCE_ATTRIBUTION}",
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

    if discovered:
        lines.extend(["", "## 数据驱动发现（文献窗外 top 5）", ""])
        lines.append(
            "自 case universe 全历史 walk-forward 扫描，排除文献窗口内信号，按 composite + 20d 回报排序。"
        )
        lines.extend(
            [
                "",
                "| ticker | as_of | composite | 20d % | 60d % | quality |",
                "|--------|-------|-----------|-------|-------|---------|",
            ]
        )
        for row in discovered:
            lines.append(
                f"| {row['ticker']} | {row['as_of']} | {row['composite_score']:.1f} | "
                f"{row.get('fwd_20d_pct', '—')} | {row.get('fwd_60d_pct', '—')} | "
                f"{row.get('quality') or '—'} |"
            )

    lines.extend(["", "## 说明", ""])
    lines.append(
        "表格「全/文献/重叠」= walk-forward 命中次数（全样本）/ 文献期内命中 / 是否与文献期重叠（✓）。"
    )
    lines.append(
        f"同一 ticker 多案例分别计；walk-forward 每 **{sample_every}** 交易日取样。"
    )
    lines.append(
        "校准参数见 `config/vcp_tm_calibrated.yaml`；生产 vcp_screen 使用 vcp-tm 默认。"
    )
    return "\n".join(lines) + "\n"


def _threshold_metrics(
    signals: list[dict],
    min_score: float,
    forward_horizons: tuple[int, ...],
) -> dict[str, float | int | None]:
    subset = _filter_signals(signals, min_score)
    out: dict[str, float | int | None] = {"n": len(subset)}
    for h in forward_horizons:
        key = f"fwd_{h}d_pct"
        vals = [float(s[key]) for s in subset if s.get(key) is not None]
        if not vals:
            out[f"mean_{h}d"] = None
            if h == 20:
                out["hit_rate_20d"] = None
            continue
        out[f"mean_{h}d"] = round(statistics.mean(vals), 2)
        if h == 20:
            out["hit_rate_20d"] = round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1)
    return out


def _literature_overlap_count(
    case_rows: list[dict],
    score_thresholds: tuple[float, ...],
    *,
    primary_thr: float = 65.0,
) -> int:
    thr_key = int(primary_thr) if primary_thr == int(primary_thr) else primary_thr
    return sum(1 for r in case_rows if r.get(f"overlap_{thr_key}"))


def build_cutover_verdict_section(
    *,
    legacy_signals: list[dict],
    tm_signals: list[dict],
    legacy_case_rows: list[dict],
    tm_case_rows: list[dict],
    score_thresholds: tuple[float, ...],
    forward_horizons: tuple[int, ...],
    target_overlap: float = 0.8,
) -> tuple[str, str]:
    """Return (markdown section, PASS|HOLD)."""
    primary_thr = 65.0 if 65.0 in score_thresholds else score_thresholds[0]
    n_cases = max(len(legacy_case_rows), len(tm_case_rows), 1)
    leg_overlap = _literature_overlap_count(legacy_case_rows, score_thresholds, primary_thr=primary_thr)
    tm_overlap = _literature_overlap_count(tm_case_rows, score_thresholds, primary_thr=primary_thr)
    leg_m = _threshold_metrics(legacy_signals, primary_thr, forward_horizons)
    tm_m = _threshold_metrics(tm_signals, primary_thr, forward_horizons)

    overlap_ok = (tm_overlap / n_cases) >= target_overlap
    hit_ok = (
        leg_m.get("hit_rate_20d") is None
        or tm_m.get("hit_rate_20d") is None
        or float(tm_m["hit_rate_20d"]) >= float(leg_m["hit_rate_20d"])
    )
    mean20_ok = (
        leg_m.get("mean_20d") is None
        or tm_m.get("mean_20d") is None
        or float(tm_m["mean_20d"]) >= float(leg_m["mean_20d"])
    )
    mean60_leg = leg_m.get("mean_60d")
    mean60_tm = tm_m.get("mean_60d")
    if mean60_leg is None or mean60_tm is None:
        mean60_ok = True
    else:
        gap = float(mean60_leg) - float(mean60_tm)
        mean60_ok = float(mean60_tm) >= float(mean60_leg) or gap < 1.0

    verdict = "PASS" if all((overlap_ok, hit_ok, mean20_ok, mean60_ok)) else "HOLD"

    def _fmt(v: float | int | None) -> str:
        return "—" if v is None else str(v)

    lines = [
        "## Cutover Verdict",
        "",
        f"**{verdict}** — vcp-tm vs legacy vcp-nse-port @ composite ≥ {int(primary_thr) if primary_thr == int(primary_thr) else primary_thr}",
        "",
        "| Metric | vcp-nse-port | vcp-tm | Required |",
        "|--------|--------------|--------|----------|",
        f"| Literature overlap | {leg_overlap}/{n_cases} | {tm_overlap}/{n_cases} | ≥ {target_overlap:.0%} |",
        f"| 20d hit rate % | {_fmt(leg_m.get('hit_rate_20d'))} | {_fmt(tm_m.get('hit_rate_20d'))} | tm ≥ legacy |",
        f"| 20d mean return % | {_fmt(leg_m.get('mean_20d'))} | {_fmt(tm_m.get('mean_20d'))} | tm ≥ legacy |",
        f"| 60d mean return % | {_fmt(leg_m.get('mean_60d'))} | {_fmt(tm_m.get('mean_60d'))} | tm ≥ legacy or gap < 1pp |",
        "",
    ]
    if verdict == "HOLD":
        reasons = []
        if not overlap_ok:
            reasons.append(f"literature overlap {tm_overlap}/{n_cases} < {target_overlap:.0%}")
        if not hit_ok:
            reasons.append("20d hit rate below legacy")
        if not mean20_ok:
            reasons.append("20d mean return below legacy")
        if not mean60_ok:
            reasons.append("60d mean return gap ≥ 1pp")
        lines.append(f"_Hold reasons: {'; '.join(reasons)}_")
        lines.append("")
    return "\n".join(lines), verdict


def main() -> int:
    parser = argparse.ArgumentParser(description="台股 VCP 金标准 walk-forward")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--tickers", default=None)
    parser.add_argument("--benchmark", default=None)
    parser.add_argument("--years", type=int, default=None)
    parser.add_argument("--min-scores", default=None)
    parser.add_argument("--sample-every", type=int, default=None)
    parser.add_argument("--forward-days", default=None)
    parser.add_argument("--use-db", action="store_true", help="读 stock_daily_bars 缓存")
    parser.add_argument(
        "--dense-literature",
        action="store_true",
        help="文献期逐日 dense scan（非 walk-forward 稀疏取样）",
    )
    parser.add_argument(
        "--calibrated",
        type=Path,
        default=None,
        help="使用 vcp_tm_calibrated.yaml 参数（先跑 literature_audit）",
    )
    parser.add_argument(
        "--compare-legacy",
        action="store_true",
        help="双轨对照 vcp-tm vs vcp-nse-port，输出 Cutover Verdict",
    )
    parser.add_argument(
        "--target-overlap",
        type=float,
        default=0.8,
        help="Cutover 文献重叠门槛（默认 0.8）",
    )
    parser.add_argument(
        "--discover",
        type=int,
        default=5,
        help="扫描文献窗外 top-N 数据驱动案例（0=关闭）",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    if not args.cases.is_file():
        print(f"ERROR: cases file not found: {args.cases}", file=sys.stderr)
        return 1

    config = load_vcp_tw_cases(args.cases)
    benchmark = args.benchmark or config.benchmark
    tickers = (
        tuple(t.strip() for t in args.tickers.split(",") if t.strip())
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

    eval_params: VcpTmParams | None = None
    lit_pad = 0
    min_composite_lit = min(score_thresholds) if score_thresholds else 65.0
    cal_path = (args.calibrated or DEFAULT_CALIBRATION).resolve()
    if cal_path.is_file():
        eval_params, min_composite_lit, lit_pad = _load_calibrated_params(cal_path)
        print(f"Using calibration: {cal_path.relative_to(PROJECT_ROOT.resolve())}", file=sys.stderr)

    prefer_finmind = True
    if not finmind_token():
        print("NOTE: 无 FINMIND_TOKEN，仅能读 DB / Yahoo 指數", file=sys.stderr)
        prefer_finmind = False

    end = date.today()
    if args.years is not None:
        start = end - timedelta(days=args.years * 366)
    else:
        start = config.fetch_start(end)

    print(f"Fetching {len(tickers)} tickers + {benchmark} ({start} ~ {end}) …")
    conn = connect(args.db) if args.use_db else None
    try:
        panels, bench_df, data_source = load_tw_panel(
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
        print("ERROR: 无 OHLCV（确认 FINMIND_TOKEN / 网络）", file=sys.stderr)
        return 1

    model_id = config.model_id if config.model_id != "vcp-nse-port" else "vcp-tm"
    if eval_params and cal_path.is_file():
        raw_cal = yaml.safe_load(cal_path.read_text(encoding="utf-8")) or {}
        model_id = str(raw_cal.get("model_id") or model_id)

    discovered: list[dict] = []
    if args.discover > 0:
        params = eval_params or VcpTmParams()
        discovered = discover_historical_cases(
            config,
            panels,
            bench_df,
            params,
            min_composite=min_composite_lit,
            sample_every=sample_every,
            top_n=args.discover,
            forward_horizons=forward_horizons,
            engine="vcp-tm",
        )

    legacy_signals: list[dict] = []
    legacy_case_rows: list[dict] = []

    if args.dense_literature:
        params = eval_params or VcpTmParams()
        all_signals = run_dense_literature_signals(
            config,
            panels,
            bench_df,
            params,
            min_composite=min_composite_lit,
            pad_days=lit_pad,
            forward_horizons=forward_horizons,
        )
        from .vcp_tw_literature_audit import count_overlaps

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
        if args.compare_legacy:
            legacy_signals = run_walkforward(
                panels,
                bench_df,
                sample_every=sample_every,
                forward_horizons=forward_horizons,
                eval_params=eval_params,
                engine="vcp-nse-port",
            )
            legacy_case_rows = summarize_case_hits(
                legacy_signals, config, score_thresholds=score_thresholds
            )
    else:
        all_signals = run_walkforward(
            panels,
            bench_df,
            sample_every=sample_every,
            forward_horizons=forward_horizons,
            eval_params=eval_params,
            engine="vcp-tm",
        )
        case_rows = summarize_case_hits(
            all_signals, config, score_thresholds=score_thresholds
        )
        sample_note = str(sample_every)
        if args.compare_legacy:
            legacy_signals = run_walkforward(
                panels,
                bench_df,
                sample_every=sample_every,
                forward_horizons=forward_horizons,
                eval_params=eval_params,
                engine="vcp-nse-port",
            )
            legacy_case_rows = summarize_case_hits(
                legacy_signals, config, score_thresholds=score_thresholds
            )

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
        discovered=discovered or None,
    )
    if args.dense_literature:
        md = md.replace(
            f"walk-forward 每 **{sample_every}** 交易日取样",
            f"文献期 **dense** 逐日扫描（{sample_note}）",
        )

    if args.compare_legacy:
        cutover_md, verdict = build_cutover_verdict_section(
            legacy_signals=legacy_signals,
            tm_signals=all_signals,
            legacy_case_rows=legacy_case_rows,
            tm_case_rows=case_rows,
            score_thresholds=score_thresholds,
            forward_horizons=forward_horizons,
            target_overlap=args.target_overlap,
        )
        md = md.rstrip() + "\n\n" + cutover_md
        print(f"  Cutover Verdict: {verdict}", file=sys.stderr)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(md, encoding="utf-8")

    for thr in score_thresholds:
        n = len(_filter_signals(all_signals, thr))
        lit = sum(1 for r in case_rows if r[f"overlap_{int(thr) if thr == int(thr) else thr}"])
        print(f"  ≥{thr}: signals={n} · literature overlap cases={lit}/{len(case_rows)}")
    if discovered:
        print(f"  discovered {len(discovered)} extra cases outside literature windows")
    print(f"  → {args.output.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
