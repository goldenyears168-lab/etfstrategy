#!/usr/bin/env python3
"""
VCP 策略回測：vcp-v1 · chunge-funnel(L4) · 盤中守穩/回落。

對照 config/vcp_tw_cases.yaml 20 例文獻期 + walk-forward 前瞻報酬 + 參數校準。

用法：
  PYTHONPATH=src python src/vcp_strategy_benchmark.py --use-db
  PYTHONPATH=src python src/vcp_strategy_benchmark.py --use-db --calibrate
"""

from __future__ import annotations

import argparse
import itertools
import statistics
import sys
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd
import yaml

from chunge_funnel_screen import ChungeFunnelParams, load_chunge_funnel_params
from finmind_client import finmind_token
from stock_db import DEFAULT_DB_PATH, PROJECT_ROOT, connect
from report_paths import REPORTS_RESEARCH
from vcp_intraday_watch import classify_intraday
from .vcp_tw_cases import DEFAULT_CASES_PATH, load_vcp_tw_cases, summarize_case_hits
from .vcp_tw_data import load_tw_panel
from .vcp_v1 import evaluate_vcp_v1
from stage_analysis import calculate_simple_trend
from vcp_nse_port.vcp_pattern import calculate_vcp
from vcp_nse_port.relative_strength import calculate_relative_strength
from vcp_nse_port.volume_pattern import calculate_volume_pattern
from vcp_nse_port.pivot_proximity import calculate_pivot_proximity
from vcp_nse_port.scorer import calculate_composite_score

DEFAULT_OUTPUT = REPORTS_RESEARCH / "vcp_strategy_benchmark.md"
DEFAULT_CALIBRATION = PROJECT_ROOT / "config" / "vcp_strategy_calibrated.yaml"
TARGET_LIT_OVERLAP = 0.80
TARGET_FWD_HIT = 0.80

CHUNGE_GRID: dict[str, tuple] = {
    "t1_depth_max": (60.0, 80.0, 95.0, 100.0),
    "contraction_ratio": (0.75, 0.85, 0.95),
    "min_rs_score": (35.0, 40.0, 50.0),
    "require_market_trend": (True, False),
    "min_score": (50.0, 55.0, 65.0),
}

V1_GRID: dict[str, tuple] = {
    "contraction_mult": (0.78, 0.85),
    "vol_dry_mult": (0.85, 0.90),
    "min_score": (50.0, 60.0, 75.0),
}


@dataclass(frozen=True)
class V1TuneParams:
    contraction_mult: float = 0.78
    vol_dry_mult: float = 0.85
    min_score: float = 50.0


def _forward_return(closes: pd.Series, idx: int, horizon: int) -> float | None:
    if idx + horizon >= len(closes):
        return None
    base = float(closes.iloc[idx])
    future = float(closes.iloc[idx + horizon])
    if base <= 0:
        return None
    return (future - base) / base * 100.0


def evaluate_chunge_l4(
    stock_df: pd.DataFrame,
    bench_df: pd.DataFrame,
    params: ChungeFunnelParams,
    *,
    liquidity_floor: float = 0.0,
) -> dict:
    """春哥漏斗 L4（VCP+RS+市況），供回測用（不含 L5-L7 基本面）。"""
    simple = calculate_simple_trend(stock_df)
    if not simple["passed"]:
        return {"passed": False, "composite_score": 0.0, "reject_reason": "L2 trend"}

    vol = calculate_volume_pattern(stock_df)
    avg_vol = float((vol.get("details") or {}).get("avg_volume_50d") or 0.0)
    if liquidity_floor > 0 and avg_vol < liquidity_floor:
        return {"passed": False, "composite_score": 0.0, "reject_reason": "L3 liquidity"}

    bench_simple = calculate_simple_trend(bench_df)
    market_ok = bool(bench_simple.get("passed"))
    vcp_kw = params.vcp_kwargs()
    vcp = calculate_vcp(stock_df, **vcp_kw)
    rs = calculate_relative_strength(stock_df, bench_df)
    rs_score = float(rs.get("score") or 0.0)
    if not vcp.get("is_vcp") or rs_score < params.min_rs_score:
        return {"passed": False, "composite_score": 0.0, "reject_reason": "L4 vcp/rs"}
    if params.require_market_trend and not market_ok:
        return {"passed": False, "composite_score": 0.0, "reject_reason": "L4 market"}

    current = float(stock_df["Close"].iloc[-1])
    pivot = float(vcp["pivot"]) if vcp.get("pivot") else current
    pivot_prox = calculate_pivot_proximity(current, pivot)
    composite = calculate_composite_score(
        trend_score=100.0,
        contraction_score=float(vcp.get("score") or 0.0),
        volume_score=float(vol.get("score") or 0.0),
        pivot_score=float(pivot_prox.get("score") or 0.0),
        rs_score=rs_score,
    )
    return {
        "passed": True,
        "composite_score": float(composite["composite_score"]),
        "pivot": pivot,
        "quality": composite.get("quality"),
        "stage": "L4_vcp",
    }


def evaluate_vcp_v1_tuned(stock_df: pd.DataFrame, tune: V1TuneParams) -> dict:
    """v1 with tunable contraction/vol thresholds."""
    base = evaluate_vcp_v1(stock_df)
    if not base.get("passed"):
        return base
    # Re-check with tuned thresholds using raw metrics
    cr = base.get("contraction_ratio")
    vr = base.get("vol_dry_ratio")
    if cr is not None and cr > tune.contraction_mult:
        return {**base, "passed": False, "composite_score": float(base.get("composite_score") or 0) * 0.5}
    if vr is not None and vr > tune.vol_dry_mult:
        return {**base, "passed": False, "composite_score": float(base.get("composite_score") or 0) * 0.5}
    score = float(base.get("composite_score") or 0.0)
    if score < tune.min_score:
        return {**base, "passed": False}
    return base


def run_walkforward_model(
    panels: dict[str, pd.DataFrame],
    bench_full: pd.DataFrame,
    *,
    evaluator: Callable[..., dict],
    eval_kwargs: dict,
    sample_every: int,
    forward_horizons: tuple[int, ...],
    min_score: float,
) -> list[dict]:
    signals: list[dict] = []
    max_h = max(forward_horizons)
    for ticker, full_df in panels.items():
        closes = full_df["Close"]
        dates = full_df["date"]
        for idx in range(200, len(full_df) - max_h - 1, sample_every):
            stock_slice = full_df.iloc[: idx + 1].copy()
            as_of = dates.iloc[idx]
            bench_slice = bench_full[bench_full["date"] <= as_of]
            if len(bench_slice) < 200:
                continue
            result = evaluator(stock_slice, bench_slice, **eval_kwargs)
            if not result.get("passed"):
                continue
            score = float(result.get("composite_score") or 0.0)
            if score < min_score:
                continue
            row = {
                "ticker": ticker,
                "as_of": str(as_of.date()),
                "composite_score": score,
                "quality": result.get("quality"),
                "pivot": result.get("pivot"),
            }
            for h in forward_horizons:
                row[f"fwd_{h}d_pct"] = _forward_return(closes, idx, h)
            signals.append(row)
    return signals


def dense_literature_scan(
    config,
    panels: dict[str, pd.DataFrame],
    bench_full: pd.DataFrame,
    *,
    evaluator: Callable[..., dict],
    eval_kwargs: dict,
    min_score: float,
    forward_horizons: tuple[int, ...],
    pad_days: int = 21,
) -> list[dict]:
    """文獻窗口逐日 dense scan（提前偵測）。"""
    signals: list[dict] = []
    max_h = max(forward_horizons)
    for case in config.cases:
        stock_df = panels.get(case.ticker)
        if stock_df is None:
            continue
        start = case.literature_start - timedelta(days=pad_days)
        end = case.literature_end
        closes = stock_df["Close"]
        dates = stock_df["date"]
        date_to_idx = {dates.iloc[i].date(): i for i in range(len(stock_df))}
        d = start
        while d <= end:
            idx = date_to_idx.get(d)
            if idx is not None and idx >= 200 and idx + max_h < len(stock_df):
                stock_slice = stock_df.iloc[: idx + 1].copy()
                bench_slice = bench_full[bench_full["date"] <= dates.iloc[idx]]
                if len(bench_slice) >= 200:
                    result = evaluator(stock_slice, bench_slice, **eval_kwargs)
                    if result.get("passed") and float(result.get("composite_score") or 0) >= min_score:
                        row = {
                            "ticker": case.ticker,
                            "case_id": case.case_id,
                            "as_of": d.isoformat(),
                            "composite_score": float(result["composite_score"]),
                        }
                        for h in forward_horizons:
                            row[f"fwd_{h}d_pct"] = _forward_return(closes, idx, h)
                        signals.append(row)
            d += timedelta(days=1)
    return signals


def discover_extra_cases(
    config,
    panels: dict[str, pd.DataFrame],
    bench_full: pd.DataFrame,
    *,
    evaluator: Callable[..., dict],
    eval_kwargs: dict,
    min_score: float,
    sample_every: int,
    top_n: int,
    forward_horizons: tuple[int, ...],
) -> list[dict]:
    lit_windows: dict[str, list[tuple[date, date]]] = {}
    for case in config.cases:
        lit_windows.setdefault(case.ticker, []).append(
            (case.literature_start, case.literature_end)
        )

    def in_lit(ticker: str, as_of: date) -> bool:
        for s, e in lit_windows.get(ticker, []):
            if s <= as_of <= e:
                return True
        return False

    all_sig = run_walkforward_model(
        panels,
        bench_full,
        evaluator=evaluator,
        eval_kwargs=eval_kwargs,
        sample_every=sample_every,
        forward_horizons=forward_horizons,
        min_score=min_score,
    )
    outside = [s for s in all_sig if not in_lit(s["ticker"], date.fromisoformat(s["as_of"]))]
    outside.sort(
        key=lambda r: (float(r["composite_score"]), float(r.get("fwd_20d_pct") or -999)),
        reverse=True,
    )
    seen: set[tuple[str, str]] = set()
    picked: list[dict] = []
    for row in outside:
        key = (row["ticker"], row["as_of"][:7])
        if key in seen:
            continue
        seen.add(key)
        picked.append(row)
        if len(picked) >= top_n:
            break
    return picked


def backtest_intraday_patterns(
    panels: dict[str, pd.DataFrame],
    *,
    sample_every: int = 1,
    forward_horizons: tuple[int, ...] = (1, 5, 20),
    fade_pct: float = 3.0,
    hold_pct: float = 2.0,
    extended_pct: float = 8.0,
) -> list[dict]:
    """以日 K 近似盤中：突破日依 high/close 分類，算前瞻報酬。"""
    rows: list[dict] = []
    max_h = max(forward_horizons)
    PIVOT_LB = 10
    for ticker, full_df in panels.items():
        closes = full_df["Close"]
        highs = full_df["High"]
        for idx in range(PIVOT_LB, len(full_df) - max_h - 1, sample_every):
            pivot = float(highs.iloc[idx - PIVOT_LB : idx].max())
            if pivot <= 0:
                continue
            close = float(closes.iloc[idx])
            day_high = float(highs.iloc[idx])
            status, dist, pullback = classify_intraday(
                pivot,
                close,
                day_high=day_high,
                fade_pullback_pct=fade_pct,
                hold_pullback_pct=hold_pct,
                extended_pct=extended_pct,
            )
            if not status.startswith("BREAKOUT"):
                continue
            row = {
                "ticker": ticker,
                "as_of": str(full_df["date"].iloc[idx].date()),
                "intraday_status": status,
                "dist_pivot_pct": dist,
                "pullback_from_high_pct": pullback,
                "pivot": pivot,
                "close": close,
                "day_high": day_high,
            }
            for h in forward_horizons:
                row[f"fwd_{h}d_pct"] = _forward_return(closes, idx, h)
            rows.append(row)
    return rows


def _hit_rate(signals: list[dict], horizon: int) -> tuple[float, int]:
    key = f"fwd_{horizon}d_pct"
    vals = [float(s[key]) for s in signals if s.get(key) is not None]
    if not vals:
        return 0.0, 0
    hit = sum(1 for v in vals if v > 0) / len(vals)
    return hit, len(vals)


def _case_success_stats(
    case_rows: list[dict],
    lit_signals: list[dict],
    *,
    thr_key: float | int,
    horizon: int = 20,
) -> tuple[int, int, int, int]:
    """Return (overlap, profitable_among_overlap, full_success, total_cases)."""
    fwd_key = f"fwd_{horizon}d_pct"
    overlap_key = f"overlap_{thr_key}"
    overlap = sum(1 for r in case_rows if r.get(overlap_key))
    profitable = 0
    full_success = 0
    for row in case_rows:
        has_overlap = bool(row.get(overlap_key))
        if not has_overlap:
            continue
        sigs = [
            s for s in lit_signals
            if s["case_id"] == row["case_id"] and s.get(fwd_key) is not None
        ]
        if any(float(s[fwd_key]) > 0 for s in sigs):
            profitable += 1
            full_success += 1
    return overlap, profitable, full_success, len(case_rows)


def _load_calibrated_params(path: Path) -> tuple[ChungeFunnelParams, float, V1TuneParams] | None:
    if not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    ch = raw.get("chunge-funnel") or {}
    v1 = raw.get("vcp-v1") or {}
    if not ch and not v1:
        return None
    base = load_chunge_funnel_params()
    cp = ch.get("params") or {}
    chunge_params = replace(
        base,
        t1_depth_max=float(cp.get("t1_depth_max", base.t1_depth_max)),
        contraction_ratio=float(cp.get("contraction_ratio", base.contraction_ratio)),
        min_rs_score=float(cp.get("min_rs_score", base.min_rs_score)),
        require_market_trend=bool(cp.get("require_market_trend", base.require_market_trend)),
    )
    chunge_min = float(ch.get("min_score") or 50.0)
    vp = v1.get("params") or {}
    v1_tune = V1TuneParams(
        contraction_mult=float(vp.get("contraction_mult", 0.78)),
        vol_dry_mult=float(vp.get("vol_dry_mult", 0.85)),
        min_score=float(vp.get("min_score", 50.0)),
    )
    return chunge_params, chunge_min, v1_tune


def _aggregate_stats(signals: list[dict], horizons: tuple[int, ...]) -> list[str]:
    if not signals:
        return ["_無信號_"]
    lines = [
        "| Horizon | Mean % | Median % | Hit >0% | N |",
        "|---------|--------|----------|---------|---|",
    ]
    for h in horizons:
        key = f"fwd_{h}d_pct"
        vals = [float(s[key]) for s in signals if s.get(key) is not None]
        if not vals:
            continue
        hit = sum(1 for v in vals if v > 0) / len(vals) * 100
        lines.append(
            f"| {h}d | {statistics.mean(vals):+.2f} | {statistics.median(vals):+.2f} | {hit:.0f}% | {len(vals)} |"
        )
    return lines


def _intraday_by_status(intra_rows: list[dict], horizons: tuple[int, ...]) -> list[str]:
    lines = ["| 型態 | N | " + " | ".join(f"{h}d hit" for h in horizons) + " |", "|------|---|" + "|".join("------:" for _ in horizons) + "|"]
    by_status: dict[str, list[dict]] = {}
    for r in intra_rows:
        by_status.setdefault(r["intraday_status"], []).append(r)
    for status, group in sorted(by_status.items()):
        cells = [status, str(len(group))]
        for h in horizons:
            hit, n = _hit_rate(group, h)
            cells.append(f"{hit * 100:.0f}%" if n else "—")
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def calibrate_chunge(
    config,
    panels: dict[str, pd.DataFrame],
    bench_full: pd.DataFrame,
    *,
    forward_horizons: tuple[int, ...],
    target_overlap: float,
    target_hit: float,
) -> tuple[ChungeFunnelParams, float, int, float, int]:
    """Grid search chunge L4 params."""
    base = load_chunge_funnel_params()
    keys = list(CHUNGE_GRID.keys())
    best = None
    best_score = -1.0
    n_cases = len(config.cases)

    for combo in itertools.product(*(CHUNGE_GRID[k] for k in keys)):
        params_dict = dict(zip(keys, combo))
        min_score = float(params_dict.pop("min_score"))
        require_mkt = bool(params_dict.pop("require_market_trend"))
        params = replace(
            base,
            t1_depth_max=float(params_dict["t1_depth_max"]),
            contraction_ratio=float(params_dict["contraction_ratio"]),
            min_rs_score=float(params_dict["min_rs_score"]),
            require_market_trend=require_mkt,
        )
        kwargs = {"params": params, "liquidity_floor": 0.0}
        lit_sigs = dense_literature_scan(
            config,
            panels,
            bench_full,
            evaluator=evaluate_chunge_l4,
            eval_kwargs=kwargs,
            min_score=min_score,
            forward_horizons=forward_horizons,
        )
        case_rows = summarize_case_hits(lit_sigs, config, score_thresholds=(min_score,))
        overlap = sum(1 for r in case_rows if r.get(f"overlap_{int(min_score) if min_score == int(min_score) else min_score}"))
        hit20, _ = _hit_rate(lit_sigs, 20)
        if overlap / n_cases < target_overlap or hit20 < target_hit:
            continue
        score = overlap * 10 + hit20 * 5 - min_score * 0.01
        if score > best_score:
            best_score = score
            best = (params, min_score, overlap, hit20, n_cases)

    if best is None:
        # fallback: maximize overlap then hit
        for combo in itertools.product(*(CHUNGE_GRID[k] for k in keys)):
            params_dict = dict(zip(keys, combo))
            min_score = float(params_dict.pop("min_score"))
            require_mkt = bool(params_dict.pop("require_market_trend"))
            params = replace(
                base,
                t1_depth_max=float(params_dict["t1_depth_max"]),
                contraction_ratio=float(params_dict["contraction_ratio"]),
                min_rs_score=float(params_dict["min_rs_score"]),
                require_market_trend=require_mkt,
            )
            kwargs = {"params": params, "liquidity_floor": 0.0}
            lit_sigs = dense_literature_scan(
                config, panels, bench_full,
                evaluator=evaluate_chunge_l4, eval_kwargs=kwargs,
                min_score=min_score, forward_horizons=forward_horizons,
            )
            case_rows = summarize_case_hits(lit_sigs, config, score_thresholds=(min_score,))
            thr_key = int(min_score) if min_score == int(min_score) else min_score
            overlap = sum(1 for r in case_rows if r.get(f"overlap_{thr_key}"))
            hit20, _ = _hit_rate(lit_sigs, 20)
            score = overlap * 10 + hit20 * 5
            if score > best_score:
                best_score = score
                best = (params, min_score, overlap, hit20, n_cases)

    assert best is not None
    return best


def calibrate_v1(
    config,
    panels: dict[str, pd.DataFrame],
    bench_full: pd.DataFrame,
    *,
    forward_horizons: tuple[int, ...],
    target_overlap: float,
    target_hit: float,
) -> tuple[V1TuneParams, int, float, int]:
    n_cases = len(config.cases)
    best = None
    best_score = -1.0

    for combo in itertools.product(*(V1_GRID[k] for k in V1_GRID)):
        tune = V1TuneParams(
            contraction_mult=float(combo[0]),
            vol_dry_mult=float(combo[1]),
            min_score=float(combo[2]),
        )

        def _eval(stock_slice, bench_slice, tune=tune):
            del bench_slice
            return evaluate_vcp_v1_tuned(stock_slice, tune)

        lit_sigs = dense_literature_scan(
            config, panels, bench_full,
            evaluator=_eval, eval_kwargs={},
            min_score=tune.min_score, forward_horizons=forward_horizons,
        )
        case_rows = summarize_case_hits(lit_sigs, config, score_thresholds=(tune.min_score,))
        thr_key = int(tune.min_score) if tune.min_score == int(tune.min_score) else tune.min_score
        overlap = sum(1 for r in case_rows if r.get(f"overlap_{thr_key}"))
        hit20, _ = _hit_rate(lit_sigs, 20)
        score = overlap * 10 + hit20 * 5
        if overlap / n_cases >= target_overlap and hit20 >= target_hit and score > best_score:
            best_score = score
            best = (tune, overlap, hit20, n_cases)
        elif best is None and score > best_score:
            best_score = score
            best = (tune, overlap, hit20, n_cases)

    assert best is not None
    return best


def build_report(
    *,
    config_path: Path,
    data_source: str,
    panels_loaded: int,
    models: dict[str, dict],
    discovered: dict[str, list[dict]],
    intra_rows: list[dict],
    calib: dict[str, object] | None,
    forward_horizons: tuple[int, ...],
) -> str:
    lines = [
        "# VCP 策略回測 · vcp-v1 · chunge-funnel · 盤中型態",
        "",
        f"- Cases: `{config_path.relative_to(PROJECT_ROOT)}`",
        f"- Data: **{data_source}** · loaded **{panels_loaded}** tickers",
        f"- 文獻目標：overlap ≥ **{TARGET_LIT_OVERLAP:.0%}** · 20d hit ≥ **{TARGET_FWD_HIT:.0%}**",
        "- **案例成功** = 文獻期內有信號且 20d 報酬 >0%；**信號 hit** = 全部 dense scan 信號的 20d >0% 比例",
        "",
    ]
    if calib:
        lines.extend(["## 校準結果", ""])
        for name, body in calib.items():
            lines.append(f"### {name}")
            if isinstance(body, dict):
                for k, v in body.items():
                    lines.append(f"- **{k}**: {v}")
            else:
                lines.append(str(body))
            lines.append("")

    for model_name, block in models.items():
        lines.extend([
            f"## {model_name}",
            "",
            f"- Walk-forward signals (score≥{block['min_score']}): **{block['wf_n']}**",
            f"- 文獻期 overlap: **{block['lit_overlap']}/{block['lit_total']}** ({block['lit_pct']:.0f}%)",
            f"- 案例成功（偵測+20d>0）: **{block['case_success']}/{block['lit_total']}** ({block['case_success_pct']:.0f}%)",
            f"- 已偵測案例 20d 獲利率: **{block['overlap_profit']}/{block['lit_overlap']}** ({block['overlap_profit_pct']:.0f}%)",
            f"- 信號級 20d hit: **{block['lit_hit20']:.0f}%** (n={block['lit_hit_n']})",
            "",
            "### Walk-forward 前瞻",
            "",
            *_aggregate_stats(block["wf_signals"], forward_horizons),
            "",
            "### 文獻期 dense scan 前瞻",
            "",
            *_aggregate_stats(block["lit_signals"], forward_horizons),
            "",
            "### 20 例文獻對照",
            "",
            "| case | ticker | 文獻期 | overlap |",
            "|------|--------|--------|---------|",
        ])
        for row in block["case_rows"]:
            ov = "✓" if row.get(f"overlap_{block['thr_key']}") else "—"
            lines.append(
                f"| {row['case_id']} | {row['ticker']} | {row['literature']} | {ov} |"
            )
        lines.append("")

    for model_name, extra in discovered.items():
        lines.extend([f"## 文獻外發現 · {model_name} (top {len(extra)})", ""])
        if not extra:
            lines.append("_無_")
        else:
            lines.extend([
                "| ticker | as_of | score | 20d % | 60d % |",
                "|--------|-------|-------|-------|-------|",
            ])
            for r in extra:
                lines.append(
                    f"| {r['ticker']} | {r['as_of']} | {r['composite_score']:.1f} "
                    f"| {r.get('fwd_20d_pct', '—')} | {r.get('fwd_60d_pct', '—')} |"
                )
        lines.append("")

    lines.extend([
        "## 盤中型態回測（日 K 近似 · pivot 突破日）",
        "",
        *_intraday_by_status(intra_rows, forward_horizons),
        "",
        "說明：以收盤價 + 當日高點近似 13:00 守穩/回落；非 tick 級回測。",
        "",
    ])
    return "\n".join(lines)


def run_benchmark(
    *,
    use_db: bool,
    db_path: Path,
    calibrate: bool,
    discover_n: int,
    output: Path,
) -> Path:
    config = load_vcp_tw_cases()
    end = date.today()
    start = config.fetch_start(end)
    tickers = config.tickers
    forward_horizons = config.forward_days
    sample_every = config.sample_every

    conn = connect(db_path) if use_db else None
    try:
        panels, bench_df, data_source = load_tw_panel(
            tickers,
            config.benchmark,
            start,
            end,
            conn=conn,
            use_db=use_db,
            prefer_finmind=bool(finmind_token()),
        )
    finally:
        if conn:
            conn.close()

    if not panels:
        raise RuntimeError("無 OHLCV 資料")

    calib_out: dict[str, object] = {}
    chunge_params = load_chunge_funnel_params()
    chunge_min = 50.0
    v1_tune = V1TuneParams()

    if calibrate:
        cp, cmin, cov, hit, tot = calibrate_chunge(
            config, panels, bench_df,
            forward_horizons=forward_horizons,
            target_overlap=TARGET_LIT_OVERLAP,
            target_hit=TARGET_FWD_HIT,
        )
        chunge_params, chunge_min = cp, cmin
        calib_out["chunge-funnel"] = {
            "params": asdict(chunge_params),
            "min_score": cmin,
            "literature_overlap": f"{cov}/{tot}",
            "lit_20d_hit": f"{hit:.0%}",
        }
        vt, vov, vhit, vtot = calibrate_v1(
            config, panels, bench_df,
            forward_horizons=forward_horizons,
            target_overlap=TARGET_LIT_OVERLAP,
            target_hit=TARGET_FWD_HIT,
        )
        v1_tune = vt
        calib_out["vcp-v1"] = {
            "params": asdict(v1_tune),
            "literature_overlap": f"{vov}/{vtot}",
            "lit_20d_hit": f"{vhit:.0%}",
        }
    elif loaded := _load_calibrated_params(DEFAULT_CALIBRATION):
        chunge_params, chunge_min, v1_tune = loaded

    models: dict[str, dict] = {}

    # chunge-funnel
    ck = {"params": chunge_params, "liquidity_floor": 0.0}
    ch_wf = run_walkforward_model(
        panels, bench_df, evaluator=evaluate_chunge_l4, eval_kwargs=ck,
        sample_every=sample_every, forward_horizons=forward_horizons, min_score=chunge_min,
    )
    ch_lit = dense_literature_scan(
        config, panels, bench_df, evaluator=evaluate_chunge_l4, eval_kwargs=ck,
        min_score=chunge_min, forward_horizons=forward_horizons,
    )
    thr = int(chunge_min) if chunge_min == int(chunge_min) else chunge_min
    ch_cases = summarize_case_hits(ch_lit, config, score_thresholds=(chunge_min,))
    ch_ov = sum(1 for r in ch_cases if r.get(f"overlap_{thr}"))
    ch_ov_n, ch_prof, ch_succ, ch_tot = _case_success_stats(ch_cases, ch_lit, thr_key=thr)
    ch_hit20, ch_hit_n = _hit_rate(ch_lit, 20)
    models["chunge-funnel (L4)"] = {
        "min_score": chunge_min,
        "thr_key": thr,
        "wf_signals": ch_wf,
        "wf_n": len(ch_wf),
        "lit_signals": ch_lit,
        "case_rows": ch_cases,
        "lit_overlap": ch_ov,
        "lit_total": len(config.cases),
        "lit_pct": ch_ov / len(config.cases) * 100,
        "case_success": ch_succ,
        "case_success_pct": ch_succ / ch_tot * 100 if ch_tot else 0,
        "overlap_profit": ch_prof,
        "overlap_profit_pct": ch_prof / ch_ov * 100 if ch_ov else 0,
        "lit_hit20": ch_hit20 * 100,
        "lit_hit_n": ch_hit_n,
    }

    # vcp-v1
    def _v1_eval(stock_slice, bench_slice, tune=v1_tune):
        del bench_slice
        return evaluate_vcp_v1_tuned(stock_slice, tune)

    v1_wf = run_walkforward_model(
        panels, bench_df, evaluator=_v1_eval, eval_kwargs={},
        sample_every=sample_every, forward_horizons=forward_horizons, min_score=v1_tune.min_score,
    )
    v1_lit = dense_literature_scan(
        config, panels, bench_df, evaluator=_v1_eval, eval_kwargs={},
        min_score=v1_tune.min_score, forward_horizons=forward_horizons,
    )
    v1_thr = int(v1_tune.min_score) if v1_tune.min_score == int(v1_tune.min_score) else v1_tune.min_score
    v1_cases = summarize_case_hits(v1_lit, config, score_thresholds=(v1_tune.min_score,))
    v1_ov = sum(1 for r in v1_cases if r.get(f"overlap_{v1_thr}"))
    _, v1_prof, v1_succ, v1_tot = _case_success_stats(v1_cases, v1_lit, thr_key=v1_thr)
    v1_hit20, v1_hit_n = _hit_rate(v1_lit, 20)
    models["vcp-v1"] = {
        "min_score": v1_tune.min_score,
        "thr_key": v1_thr,
        "wf_signals": v1_wf,
        "wf_n": len(v1_wf),
        "lit_signals": v1_lit,
        "case_rows": v1_cases,
        "lit_overlap": v1_ov,
        "lit_total": len(config.cases),
        "lit_pct": v1_ov / len(config.cases) * 100,
        "case_success": v1_succ,
        "case_success_pct": v1_succ / v1_tot * 100 if v1_tot else 0,
        "overlap_profit": v1_prof,
        "overlap_profit_pct": v1_prof / v1_ov * 100 if v1_ov else 0,
        "lit_hit20": v1_hit20 * 100,
        "lit_hit_n": v1_hit_n,
    }

    discovered = {
        "chunge-funnel": discover_extra_cases(
            config, panels, bench_df, evaluator=evaluate_chunge_l4, eval_kwargs=ck,
            min_score=chunge_min, sample_every=sample_every, top_n=discover_n,
            forward_horizons=forward_horizons,
        ),
        "vcp-v1": discover_extra_cases(
            config, panels, bench_df, evaluator=_v1_eval, eval_kwargs={},
            min_score=v1_tune.min_score, sample_every=sample_every, top_n=discover_n,
            forward_horizons=forward_horizons,
        ),
    }

    intra_rows = backtest_intraday_patterns(panels, sample_every=5, forward_horizons=(1, 5, 20))

    md = build_report(
        config_path=DEFAULT_CASES_PATH,
        data_source=data_source,
        panels_loaded=len(panels),
        models=models,
        discovered=discovered,
        intra_rows=intra_rows,
        calib=calib_out if calib_out else None,
        forward_horizons=forward_horizons,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(md, encoding="utf-8")

    if calibrate and calib_out:
        cal_path = PROJECT_ROOT / "config" / "vcp_strategy_calibrated.yaml"
        cal_path.write_text(yaml.safe_dump(calib_out, allow_unicode=True, sort_keys=False), encoding="utf-8")

    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="VCP 策略回測（v1 · chunge · 盤中）")
    parser.add_argument("--use-db", action="store_true")
    parser.add_argument("--calibrate", action="store_true", help="網格校準至 overlap/hit >=80%%")
    parser.add_argument("--discover", type=int, default=5)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    try:
        path = run_benchmark(
            use_db=args.use_db,
            db_path=args.db,
            calibrate=args.calibrate,
            discover_n=args.discover,
            output=args.output,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
