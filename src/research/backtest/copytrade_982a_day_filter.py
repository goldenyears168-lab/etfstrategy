"""00981A 跟單 · 982A 重疊調倉日 filter（方向 A：日曆層 · 全 basket）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import date as date_cls

from .copytrade_backtest import (
    PRIMARY_ALPHA_FIELD,
    SECONDARY_ALPHA_FIELD,
    _build_day_results,
    _matrix_row_key,
    _resolve_matrix_strategy_params,
    _summarize_day_run,
    _summarize_leg_level,
    group_signals_by_date,
    iter_copytrade_signals,
    load_stock_beta_map,
)
from stock_db import (
    list_etf_snapshot_dates,
    persist_copytrade_nlegs_filter_compare,
    persist_copytrade_research_conclusions,
)

DEFAULT_CONSENSUS_ETF = "00982A"

DAY_FILTER_SPECS: dict[str, tuple[str, str]] = {
    "all": ("基準（全部訊號日）", "all"),
    "day_982a_overlap": ("僅 982A 重疊調倉日 · 全 basket", "overlap"),
    "day_982a_skip": ("跳過 982A 重疊日", "skip"),
}


def resolve_aligned_window_start(
    conn: sqlite3.Connection,
    consensus_etf: str,
    window_start: str | None,
) -> str | None:
    dates = list_etf_snapshot_dates(conn, consensus_etf)
    if not dates:
        return window_start
    peer_start = dates[-1]
    if window_start is None:
        return peer_start
    return max(window_start, peer_start)


def build_982a_overlap_day_set(
    conn: sqlite3.Connection,
    target_etf: str,
    consensus_etf: str,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
) -> set[str]:
    """981A 訊號日 T：當日任一 leg 與 peer ETF 同日同股新进/加码。"""
    peer_sigs = iter_copytrade_signals(
        conn,
        consensus_etf,
        window_start=window_start,
        window_end=window_end,
    )
    peer_by_date: dict[str, set[str]] = {}
    for s in peer_sigs:
        peer_by_date.setdefault(s.signal_date, set()).add(s.stock_id)

    overlap: set[str] = set()
    for sig in iter_copytrade_signals(
        conn,
        target_etf,
        window_start=window_start,
        window_end=window_end,
    ):
        peer_stocks = peer_by_date.get(sig.signal_date)
        if peer_stocks and sig.stock_id in peer_stocks:
            overlap.add(sig.signal_date)
    return overlap


def filter_grouped_by_overlap_days(
    grouped: dict[str, list],
    overlap_days: set[str],
    mode: str,
) -> dict[str, list]:
    if mode == "all":
        return grouped
    if mode == "overlap":
        return {d: legs for d, legs in grouped.items() if d in overlap_days}
    if mode == "skip":
        return {d: legs for d, legs in grouped.items() if d not in overlap_days}
    raise ValueError(f"unknown overlap day mode: {mode}")


def _independent_significance(
    sample_a: list[float],
    sample_b: list[float],
) -> dict[str, float | None]:
    if len(sample_a) < 3 or len(sample_b) < 3:
        return {"p_value_mannwhitney": None, "mean_a": None, "mean_b": None}
    mean_a = sum(sample_a) / len(sample_a)
    mean_b = sum(sample_b) / len(sample_b)
    try:
        from scipy.stats import mannwhitneyu

        _, p = mannwhitneyu(sample_a, sample_b, alternative="two-sided")
    except Exception:
        return {
            "p_value_mannwhitney": None,
            "mean_a": round(mean_a, 2),
            "mean_b": round(mean_b, 2),
        }
    return {
        "p_value_mannwhitney": round(float(p), 4) if p == p else None,
        "mean_a": round(mean_a, 2),
        "mean_b": round(mean_b, 2),
    }


def run_982a_day_filter_study(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    strategy_id: str = "L1H9",
    consensus_etf: str = DEFAULT_CONSENSUS_ETF,
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
    """982A 重疊調倉日（方向 A）：僅在重疊日跟全部 981A leg vs 基準。"""
    entry_lag_days, hold_trading_days = _resolve_matrix_strategy_params(
        strategy_id,
        entry_lag_days=entry_lag_days,
        hold_trading_days=hold_trading_days,
    )
    aligned_start = resolve_aligned_window_start(conn, consensus_etf, window_start)
    overlap_days = build_982a_overlap_day_set(
        conn,
        etf_code,
        consensus_etf,
        window_start=aligned_start,
        window_end=window_end,
    )

    all_signals = iter_copytrade_signals(
        conn,
        etf_code,
        window_start=aligned_start,
        window_end=window_end,
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

    filter_results: dict[str, dict[str, object]] = {}
    compare_rows: list[dict] = []
    n_total_days = len(grouped_all)

    for filter_id, (filter_label, mode) in DAY_FILTER_SPECS.items():
        grouped = filter_grouped_by_overlap_days(grouped_all, overlap_days, mode)
        day_results = _build_day_results(conn, grouped, **build_kw)
        summary = _summarize_day_run(day_results, conn)
        leg_stats = _summarize_leg_level(day_results)
        n_in = len(grouped)
        n_excluded = n_total_days - n_in if mode != "all" else 0
        filter_results[filter_id] = {
            "filter_label": filter_label,
            "summary": summary,
            "leg_stats": leg_stats,
            "n_signal_days_in_filter": n_in,
            "n_signal_days_excluded": n_excluded,
        }
        compare_rows.append(
            {
                "etf_code": etf_code,
                "strategy_id": strategy_id,
                "filter_id": filter_id,
                "filter_label": filter_label,
                "capital_ntd": capital_ntd,
                "entry_lag_days": entry_lag_days,
                "hold_trading_days": hold_trading_days,
                "n_signal_days_in_filter": n_in,
                "n_signal_days_excluded": n_excluded,
                "leg_win_rate_gross_pct": leg_stats.get("leg_win_rate_gross_pct"),
                "leg_n_complete": leg_stats.get("leg_n_complete") or 0,
                **summary,
            }
        )

    base = filter_results["all"]["summary"]
    overlap_sum = filter_results["day_982a_overlap"]["summary"]
    skip_sum = filter_results["day_982a_skip"]["summary"]

    wr_deltas: dict[str, float | None] = {}
    base_wr = base.get("win_rate_vs_bench_pct")
    for fid in ("day_982a_overlap", "day_982a_skip"):
        s = filter_results[fid]["summary"]
        if base_wr is not None and s.get("win_rate_vs_bench_pct") is not None:
            wr_deltas[fid] = round(
                float(s["win_rate_vs_bench_pct"]) - float(base_wr), 2
            )
        else:
            wr_deltas[fid] = None

    complete_base = [d for d in baseline_days if d.status == "complete"]
    ov_alpha = [d.alpha_ntd for d in complete_base if d.signal_date in overlap_days]
    no_alpha = [
        d.alpha_ntd for d in complete_base if d.signal_date not in overlap_days
    ]
    day_bucket_test = _independent_significance(ov_alpha, no_alpha)

    capture_pct = round(
        100.0 * len(overlap_days) / n_total_days if n_total_days else 0.0, 1
    )
    avg_legs_overlap = round(
        sum(len(grouped_all[d]) for d in overlap_days if d in grouped_all)
        / len(overlap_days)
        if overlap_days
        else 0.0,
        1,
    )
    non_overlap = set(grouped_all) - overlap_days
    avg_legs_non = round(
        sum(len(grouped_all[d]) for d in non_overlap) / len(non_overlap)
        if non_overlap
        else 0.0,
        1,
    )

    bid = batch_id or (
        f"{etf_code.lower()}-982a-day-filter-{strategy_id.lower()}-"
        f"{date_cls.today().strftime('%Y%m%d')}"
    )

    ov_wr = wr_deltas.get("day_982a_overlap")
    ov_wr_s = f"{ov_wr:+} pp" if ov_wr is not None else "—"
    primary_alpha_delta = float(overlap_sum[PRIMARY_ALPHA_FIELD]) - float(
        base[PRIMARY_ALPHA_FIELD]
    )
    secondary_alpha_delta = float(overlap_sum[SECONDARY_ALPHA_FIELD]) - float(
        base[SECONDARY_ALPHA_FIELD]
    )

    if ov_wr is not None and ov_wr > 0 and primary_alpha_delta > 0:
        verdict = "採納"
    elif ov_wr is not None and ov_wr > 5 and secondary_alpha_delta > 0:
        verdict = "探索採納（Secondary 單池）"
    else:
        verdict = "探索採納（日曆監控）" if ov_wr and ov_wr > 5 else "拒絕全局"

    conclusion = (
        f"{strategy_id} 982A 重疊調倉日 filter（方向 A · 重疊日跟全 basket）："
        f"對齊窗口 {aligned_start} 起 · 重疊日 **{len(overlap_days)}** / {n_total_days}（{capture_pct}%）· "
        f"重疊日平均 {avg_legs_overlap} leg vs 非重疊 {avg_legs_non} leg。"
        f"基準勝率 {base['win_rate_vs_bench_pct']}% · 累計 α {base[PRIMARY_ALPHA_FIELD]:+,.0f} · "
        f"单池 {base[SECONDARY_ALPHA_FIELD]:+,.0f}。"
        f"**僅重疊日**：勝率 {overlap_sum['win_rate_vs_bench_pct']}%（Δ {ov_wr_s}）· "
        f"累計 α {overlap_sum[PRIMARY_ALPHA_FIELD]:+,.0f}（Δ {primary_alpha_delta:+,.0f}）· "
        f"单池 {overlap_sum[SECONDARY_ALPHA_FIELD]:+,.0f}（Δ {secondary_alpha_delta:+,.0f}）。"
        f"重疊 vs 非重疊日 α：mean {day_bucket_test.get('mean_a')} vs "
        f"{day_bucket_test.get('mean_b')} · p(MW)={day_bucket_test.get('p_value_mannwhitney')}。"
        f"→ **{verdict}**。"
    )

    details = {
        "consensus_etf": consensus_etf,
        "aligned_window_start": aligned_start,
        "n_overlap_days": len(overlap_days),
        "n_total_signal_days": n_total_days,
        "capture_pct": capture_pct,
        "avg_legs_overlap": avg_legs_overlap,
        "avg_legs_non_overlap": avg_legs_non,
        "baseline": base,
        "overlap_only": overlap_sum,
        "skip_overlap": skip_sum,
        "filters": filter_results,
        "win_rate_delta_pp": wr_deltas,
        "primary_alpha_delta": primary_alpha_delta,
        "secondary_alpha_delta": secondary_alpha_delta,
        "overlap_vs_nonoverlap_day_alpha": day_bucket_test,
        "verdict": verdict,
        "rule": (
            f"signal_date 當日任一 {etf_code} leg 與 {consensus_etf} 同日同股 add → "
            "當日跟全部 leg（非僅共識 leg）"
        ),
    }

    if persist:
        persist_copytrade_nlegs_filter_compare(conn, bid, compare_rows)
        persist_copytrade_research_conclusions(
            conn,
            bid,
            [
                {
                    "etf_code": etf_code,
                    "analysis_type": "982a_day_filter",
                    "entry_row": _matrix_row_key(strategy_id) or "L1",
                    "metric_key": "day_982a_overlap",
                    "horizon": hold_trading_days,
                    "metric_value": wr_deltas.get("day_982a_overlap"),
                    "conclusion_zh": conclusion,
                    "details_json": json.dumps(details, ensure_ascii=False),
                }
            ],
            replace_types=("982a_day_filter",),
        )

    return {
        "batch_id": bid,
        "strategy_id": strategy_id,
        "consensus_etf": consensus_etf,
        "aligned_window_start": aligned_start,
        "n_overlap_days": len(overlap_days),
        "capture_pct": capture_pct,
        "avg_legs_overlap": avg_legs_overlap,
        "avg_legs_non_overlap": avg_legs_non,
        "baseline": base,
        "overlap_only": overlap_sum,
        "skip_overlap": skip_sum,
        "filters": filter_results,
        "win_rate_delta_pp": wr_deltas,
        "primary_alpha_delta": primary_alpha_delta,
        "secondary_alpha_delta": secondary_alpha_delta,
        "overlap_vs_nonoverlap_day_alpha": day_bucket_test,
        "verdict": verdict,
        "conclusion_zh": conclusion,
        "details": details,
    }


def format_982a_day_filter_markdown(result: dict[str, object]) -> str:
    base = result["baseline"]
    overlap = result["overlap_only"]
    skip = result["skip_overlap"]
    wr_d = result["win_rate_delta_pp"]
    bucket = result["overlap_vs_nonoverlap_day_alpha"]

    lines = [
        "# 00981A 982A 重疊調倉日 Filter（方向 A · 日曆層）",
        "",
        f"> batch `{result['batch_id']}` · 策略 {result['strategy_id']} · "
        f"peer `{result['consensus_etf']}` · 窗口 {result['aligned_window_start']} 起",
        "",
        "## 假說",
        "",
        "982A 同日加碼標記「多 ETF 同步調倉日」→ 當日跟 **981A 全 basket**（非僅共識 leg）。",
        "",
        "## 樣本",
        "",
        f"- 重疊訊號日：**{result['n_overlap_days']}** / "
        f"{result['details']['n_total_signal_days']}（捕獲 {result['capture_pct']}%）",
        f"- 重疊日平均 leg：**{result['avg_legs_overlap']}** · "
        f"非重疊：**{result['avg_legs_non_overlap']}**",
        "",
        "## 風控策略回測",
        "",
        "| 策略 | 訊號日 | 異動檔數 | 勝率% | Δ pp | 累計 α | 單池實現超額 |",
        "|------|--------|------|---------|------|--------|------------|",
    ]
    for fid in DAY_FILTER_SPECS:
        row = result["filters"][fid]
        s = row["summary"]
        delta = 0.0 if fid == "all" else (wr_d.get(fid) or 0.0)
        lines.append(
            f"| {row['filter_label']} | {s['n_complete_days']} | {s['n_legs']} | "
            f"{s['win_rate_vs_bench_pct']}% | {delta:+} | "
            f"{s['total_alpha_ntd']:+,.0f} | {s['recycled_total_alpha_ntd']:+,.0f} |"
        )

    lines.extend(
        [
            "",
            "## 重疊日 vs 非重疊日（基準 basket · 獨立樣本）",
            "",
            f"- 重疊日 mean α：**{bucket.get('mean_a')}** NTD · "
            f"非重疊：**{bucket.get('mean_b')}** NTD",
            f"- Mann-Whitney p=**{bucket.get('p_value_mannwhitney')}**",
            "",
            "## 判決",
            "",
            f"**{result['verdict']}**",
            "",
            f"- Primary Δ勝率：{wr_d.get('day_982a_overlap'):+} pp · "
            f"Δ累計 α：{result['primary_alpha_delta']:+,.0f}",
            f"- Secondary Δ单池 α：{result['secondary_alpha_delta']:+,.0f}",
            "",
            "## 結論",
            "",
            str(result["conclusion_zh"]),
            "",
        ]
    )
    return "\n".join(lines)
