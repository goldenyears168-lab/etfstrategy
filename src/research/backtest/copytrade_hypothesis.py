"""H1（訊號日異動檔數）假說驗證 — Primary: skip_5_10。"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date as date_cls
from typing import Callable

from .copytrade_backtest import (
    WEIGHT_FAIL_MULT,
    WEIGHT_PASS_MULT,
    CopytradeSignal,
    _build_day_results,
    _matrix_row_key,
    _paired_action_diffs,
    _paired_significance,
    _resolve_matrix_strategy_params,
    _summarize_day_run,
    _summarize_leg_level,
    _two_proportion_ztest,
    group_signals_by_date,
    iter_copytrade_signals,
    load_stock_beta_map,
    primary_alpha_improved,
)

H1_FILTER_SPECS: dict[str, tuple[str, Callable[[int], bool] | None]] = {
    "all": ("基準（無篩選）", None),
    "only_1": ("僅 1 檔異動", lambda n: n == 1),
    "only_2_4": ("2–4 檔異動", lambda n: 2 <= n <= 4),
    "only_1_4": ("≤4 檔異動", lambda n: n <= 4),
    "skip_5_10": ("跳過單日 5–10 檔異動", lambda n: not (5 <= n <= 10)),
    "only_5_10": ("僅 5–10 檔異動（反向）", lambda n: 5 <= n <= 10),
    "only_11plus": ("≥11 檔異動", lambda n: n >= 11),
}


def leg_count_bucket(n_legs: int) -> str:
    if n_legs == 1:
        return "1"
    if 2 <= n_legs <= 4:
        return "2-4"
    if 5 <= n_legs <= 10:
        return "5-10"
    return "11+"


def filter_grouped_by_day_leg_count(
    grouped: dict[str, list[CopytradeSignal]],
    predicate: Callable[[int], bool],
) -> dict[str, list[CopytradeSignal]]:
    return {
        signal_date: legs
        for signal_date, legs in grouped.items()
        if predicate(len(legs))
    }


def _mann_whitney_p(a: list[float], b: list[float]) -> float | None:
    if len(a) < 5 or len(b) < 5:
        return None
    try:
        from scipy.stats import mannwhitneyu

        _, p = mannwhitneyu(a, b, alternative="two-sided")
        return round(float(p), 4)
    except Exception:
        return None


def analyze_leg_count_buckets(
    day_results: list,
) -> list[dict[str, object]]:
    buckets: dict[str, list[float]] = defaultdict(list)
    excess: dict[str, list[float]] = defaultdict(list)
    wins: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    for d in day_results:
        if d.status != "complete":
            continue
        b = leg_count_bucket(d.n_legs)
        buckets[b].append(float(d.alpha_ntd))
        excess[b].append(float(d.return_pct) - float(d.bench_return_pct))
        counts[b] += 1
        if d.alpha_ntd > 0:
            wins[b] += 1
    out: list[dict[str, object]] = []
    for b in ("1", "2-4", "5-10", "11+"):
        n = counts.get(b, 0)
        if not n:
            continue
        alphas = buckets[b]
        out.append(
            {
                "bucket": b,
                "n_days": n,
                "win_alpha_pct": round(wins[b] / n * 100.0, 2),
                "avg_alpha_ntd": round(sum(alphas) / n, 2),
                "sum_alpha_ntd": round(sum(alphas), 2),
                "avg_excess_pct": round(sum(excess[b]) / n, 4),
            }
        )
    return out


def leg_count_contrasts(
    day_results: list,
) -> dict[str, object]:
    by_bucket: dict[str, list[float]] = defaultdict(list)
    for d in day_results:
        if d.status != "complete":
            continue
        by_bucket[leg_count_bucket(d.n_legs)].append(float(d.alpha_ntd))
    c1 = _mann_whitney_p(by_bucket.get("5-10", []), by_bucket.get("2-4", []))
    c2 = _mann_whitney_p(by_bucket.get("5-10", []), by_bucket.get("11+", []))
    return {
        "c1_5_10_vs_2_4": {"p_value": c1, "n_a": len(by_bucket.get("5-10", [])), "n_b": len(by_bucket.get("2-4", []))},
        "c2_5_10_vs_11plus": {"p_value": c2, "n_a": len(by_bucket.get("5-10", [])), "n_b": len(by_bucket.get("11+", []))},
    }


def _adopted_filter(
    base: dict,
    summary: dict,
    *,
    paired_p: float | None,
    require_paired: bool,
) -> bool:
    base_wr = base.get("win_rate_vs_bench_pct")
    wr = summary.get("win_rate_vs_bench_pct")
    if base_wr is None or wr is None:
        return False
    d_wr = float(wr) - float(base_wr)
    alpha_ok = primary_alpha_improved(summary, base)
    if require_paired:
        return d_wr > 0 and alpha_ok and paired_p is not None and paired_p < 0.05
    return d_wr > 0 and alpha_ok


def run_leg_count_filter_study(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    strategy_id: str = "L1H9",
    entry_lag_days: int = 0,
    hold_trading_days: int = 9,
    entry_price_mode: str = "open",
    capital_ntd: float = 10_000.0,
    cost_bps: float = 0.0,
    window_start: str | None = None,
    window_end: str | None = None,
    batch_id: str | None = None,
    persist: bool = True,
) -> dict[str, object]:
    from stock_db import (
        persist_copytrade_nlegs_filter_compare,
        persist_copytrade_research_conclusions,
    )

    entry_lag_days, hold_trading_days = _resolve_matrix_strategy_params(
        strategy_id,
        entry_lag_days=entry_lag_days,
        hold_trading_days=hold_trading_days,
    )
    all_signals = list(
        iter_copytrade_signals(conn, etf_code, window_start=window_start, window_end=window_end)
    )
    grouped_all = group_signals_by_date(all_signals)
    beta_map, _ = load_stock_beta_map(conn)
    build_kw = dict(
        capital_ntd=capital_ntd,
        entry_lag_days=entry_lag_days,
        hold_trading_days=hold_trading_days,
        cost_bps=cost_bps,
        entry_price_mode=entry_price_mode,
        beta_map=beta_map,
    )
    baseline_days = _build_day_results(conn, grouped_all, **build_kw)
    baseline_sum = _summarize_day_run(baseline_days, conn)
    buckets = analyze_leg_count_buckets(baseline_days)
    contrasts = leg_count_contrasts(baseline_days)

    filter_results: dict[str, dict[str, object]] = {}
    compare_rows: list[dict] = []

    for filter_id, (filter_label, predicate) in H1_FILTER_SPECS.items():
        if predicate is None:
            grouped = grouped_all
            n_excluded = 0
        else:
            grouped = filter_grouped_by_day_leg_count(grouped_all, predicate)
            n_excluded = sum(
                1
                for legs in grouped_all.values()
                if legs and not predicate(len(legs))
            )
        day_results = _build_day_results(conn, grouped, **build_kw)
        summary = _summarize_day_run(day_results, conn)
        leg_stats = _summarize_leg_level(day_results)
        paired = _paired_action_diffs(baseline_days, day_results)
        sig_alpha = _paired_significance(paired["alpha_ntd"])
        filter_results[filter_id] = {
            "filter_label": filter_label,
            "summary": summary,
            "leg_stats": leg_stats,
            "paired_alpha_ntd": sig_alpha,
            "paired_n_days": len(paired["dates"]),
            "n_signal_days_excluded": n_excluded,
        }
        compare_rows.append(
            {
                "etf_code": etf_code,
                "strategy_id": strategy_id,
                "filter_id": filter_id,
                "filter_label": filter_label,
                "leg_bucket_spec": filter_id,
                "capital_ntd": capital_ntd,
                "entry_lag_days": entry_lag_days,
                "hold_trading_days": hold_trading_days,
                "n_signal_days_in_filter": summary.get("n_complete_days") or 0,
                "n_signal_days_excluded": n_excluded,
                "leg_win_rate_gross_pct": leg_stats.get("leg_win_rate_gross_pct"),
                "leg_n_complete": leg_stats.get("leg_n_complete") or 0,
                **summary,
            }
        )

    base = filter_results["all"]["summary"]
    primary = filter_results.get("skip_5_10", {})
    primary_sum = primary.get("summary", {})
    base_wr = float(base.get("win_rate_vs_bench_pct") or 0)
    pri_wr = float(primary_sum.get("win_rate_vs_bench_pct") or 0)
    wr_delta = round(pri_wr - base_wr, 2)
    adopted = _adopted_filter(
        base,
        primary_sum,
        paired_p=None,
        require_paired=False,
    )

    bid = batch_id or (
        f"{etf_code.lower()}-h1-legcount-{strategy_id.lower()}-"
        f"{date_cls.today().strftime('%Y%m%d')}"
    )
    conclusion = (
        f"{strategy_id} H1 訊號日異動檔數研究：基準勝率 {base['win_rate_vs_bench_pct']}%"
        f"（{base['n_complete_days']} 日）。"
        f"**skip_5_10**：勝率 {primary_sum.get('win_rate_vs_bench_pct')}%"
        f"（Δ {wr_delta:+} pp · {primary_sum.get('n_complete_days')} 日），"
        f"累計 α {primary_sum.get('total_alpha_ntd'):+,.0f}"
        f"（基準 {base.get('total_alpha_ntd'):+,.0f}；"
        f"單池 {primary_sum.get('recycled_total_alpha_ntd'):+,.0f}）。"
        f"分桶對照 5-10 vs 2-4 p={contrasts['c1_5_10_vs_2_4']['p_value']}。"
        f"**採納**（Δ勝率>0 且累計α升）：{'是' if adopted else '否'}。"
    )

    details = {
        "hypothesis": "H1-leg-count",
        "buckets": buckets,
        "contrasts": contrasts,
        "baseline": base,
        "filters": filter_results,
        "win_rate_delta_pp": {
            fid: (
                round(
                    float(filter_results[fid]["summary"]["win_rate_vs_bench_pct"])
                    - base_wr,
                    2,
                )
                if fid != "all"
                and filter_results[fid]["summary"].get("win_rate_vs_bench_pct") is not None
                else 0.0
            )
            for fid in H1_FILTER_SPECS
        },
        "adopted_primary": adopted,
    }

    if persist:
        persist_copytrade_nlegs_filter_compare(conn, bid, compare_rows)
        persist_copytrade_research_conclusions(
            conn,
            bid,
            [
                {
                    "etf_code": etf_code,
                    "analysis_type": "hypothesis_h1_leg_count",
                    "entry_row": _matrix_row_key(strategy_id) or "L1",
                    "metric_key": "skip_5_10",
                    "horizon": hold_trading_days,
                    "metric_value": wr_delta,
                    "conclusion_zh": conclusion,
                    "details_json": json.dumps(details, ensure_ascii=False),
                }
            ],
            replace_types=("hypothesis_h1_leg_count",),
        )

    return {
        "batch_id": bid,
        "hypothesis_id": "H1",
        "strategy_id": strategy_id,
        "baseline": base,
        "filters": filter_results,
        "buckets": buckets,
        "contrasts": contrasts,
        "win_rate_delta_pp": details["win_rate_delta_pp"],
        "adopted_primary": adopted,
        "conclusion_zh": conclusion,
        "details": details,
    }


def run_hypothesis_studies(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    strategy_id: str = "L1H9",
    capital_ntd: float = 10_000.0,
    cost_bps: float = 0.0,
    window_start: str | None = None,
    window_end: str | None = None,
    persist: bool = True,
) -> dict[str, object]:
    h1 = run_leg_count_filter_study(
        conn,
        etf_code,
        strategy_id=strategy_id,
        capital_ntd=capital_ntd,
        cost_bps=cost_bps,
        window_start=window_start,
        window_end=window_end,
        persist=persist,
    )
    return {"H1": h1}


def format_leg_count_markdown(result: dict[str, object]) -> str:
    base = result["baseline"]
    filters = result["filters"]
    buckets = result["buckets"]
    contrasts = result["contrasts"]
    wr_d = result["win_rate_delta_pp"]
    lines = [
        f"# 00981A H1 訊號日異動檔數研究",
        "",
        f"> batch `{result['batch_id']}` · 策略 {result['strategy_id']}",
        "",
        "## Phase 0：分桶描述（基準 L1H9）",
        "",
        "| bucket | n 日 | 勝率% | 均 α | 累計 α | 均超額% |",
        "|--------|------|---------|------|--------|---------|",
    ]
    for b in buckets:
        lines.append(
            f"| {b['bucket']} | {b['n_days']} | {b['win_alpha_pct']}% | "
            f"{b['avg_alpha_ntd']:+,.0f} | {b['sum_alpha_ntd']:+,.0f} | {b['avg_excess_pct']:+.2f}% |"
        )
    c1 = contrasts["c1_5_10_vs_2_4"]
    c2 = contrasts["c2_5_10_vs_11plus"]
    lines.extend(
        [
            "",
            f"- 5-10 vs 2-4 Mann-Whitney p={c1['p_value']} (n={c1['n_a']}/{c1['n_b']})",
            f"- 5-10 vs 11+ Mann-Whitney p={c2['p_value']} (n={c2['n_a']}/{c2['n_b']})",
            "",
            "## 篩選器回測（訊號日層 · 整日保留/跳過）",
            "",
            "| filter | 訊號日 | 異動檔數 | 勝率% | Δ vs 基準 | 累計α | 單池實現超額 |",
            "|--------|--------|------|---------|-----------|-------|-----------|",
        ]
    )
    for fid, fr in filters.items():
        s = fr["summary"]
        d = wr_d.get(fid)
        d_s = f"{d:+}" if d is not None and fid != "all" else "—"
        lines.append(
            f"| {fid} | {s['n_complete_days']} | {s['n_legs']} | "
            f"{s['win_rate_vs_bench_pct']}% | {d_s} pp | "
            f"{s['total_alpha_ntd']:+,.0f} | {s['recycled_total_alpha_ntd']:+,.0f} |"
        )
    lines.extend(
        [
            "",
            "## 結論",
            "",
            result["conclusion_zh"],
            "",
            f"**Primary（skip_5_10）採納**：{'✅' if result['adopted_primary'] else '❌'}",
        ]
    )
    return "\n".join(lines)


def format_hypothesis_combined_markdown(results: dict[str, object]) -> str:
    return format_leg_count_markdown(results["H1"])
