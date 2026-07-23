"""ABC v3+F1 · gap 截面 vs 时序分解 · per-stock grouped factor scan.

Decomposes MV gap (W20−W5, W5−W3) into:

  - **截面（between-stock）**：各股票在样本内的平均 gap vs 平均回报；或按
    股票历史 gap 振幅分群后，在群内重跑 factor scan。
  - **时序（within-stock）**：leg 的 gap 减去该股票样本内均值（within 偏差），
    或 per-stock 时序相关再 meta 汇总。

Reuses leg collection + normalization from ``abc_v3_f1_entry_factor_scan``.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from statistics import mean, median
from typing import Any

from research.backtest.abc_v3_f1_entry_factor_scan import (
    FACTOR_COLUMNS,
    attach_normalized_gate_features,
    build_stock_daily_gap_rv_series,
    scan_factor_correlations,
)
from research.backtest.abc_v3_f1_entry_gate_tp_sweep import collect_abc_v3_f1_raw_legs
from research.backtest.finpilot_local_backtest import load_price_panels
from project_config import DEFAULT_ETF_CODES

SCHEMA = "abc_v3_f1_entry_gap_decomposition-v1"

WITHIN_BETWEEN_COLS: tuple[str, ...] = (
    "gap_w20_w5_within",
    "gap_w5_w3_within",
    "gap_w20_w5_between",
    "gap_w5_w3_between",
)

STOCK_LEVEL_COLS: tuple[str, ...] = (
    "stock_mean_gap_w20_w5",
    "stock_mean_gap_w5_w3",
    "stock_mean_gap_w20_w5_eod",
    "stock_mean_gap_w5_w3_eod",
)


def _stock_event_means(legs: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Per-stock mean gap / return across events in the sample window."""
    acc: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"gap_w20_w5": [], "gap_w5_w3": [], "return_pct": []}
    )
    for leg in legs:
        sid = str(leg.get("stock_id"))
        g20 = leg.get("gap_w20_w5_raw")
        g53 = leg.get("gap_w5_w3_raw")
        ret = leg.get("return_pct")
        if g20 is not None:
            acc[sid]["gap_w20_w5"].append(float(g20))
        if g53 is not None:
            acc[sid]["gap_w5_w3"].append(float(g53))
        if ret is not None:
            acc[sid]["return_pct"].append(float(ret))
    out: dict[str, dict[str, float]] = {}
    for sid, vals in acc.items():
        row: dict[str, float] = {}
        if vals["gap_w20_w5"]:
            row["gap_w20_w5"] = mean(vals["gap_w20_w5"])
        if vals["gap_w5_w3"]:
            row["gap_w5_w3"] = mean(vals["gap_w5_w3"])
        if vals["return_pct"]:
            row["return_pct"] = mean(vals["return_pct"])
        row["n_events"] = float(len(vals["return_pct"]))
        out[sid] = row
    return out


def _stock_baseline_gap_means(
    baseline: dict[str, list[dict[str, Any]]],
    *,
    before_date: str,
    window: int = 60,
) -> dict[str, dict[str, float]]:
    """Per-stock trailing EOD gap mean strictly before ``before_date``."""
    out: dict[str, dict[str, float]] = {}
    for sid, series in baseline.items():
        rows = [r for r in series if r["date"] < before_date][-window:]
        g20 = [float(r["gap_w20_w5_eod"]) for r in rows if r.get("gap_w20_w5_eod") is not None]
        g53 = [float(r["gap_w5_w3_eod"]) for r in rows if r.get("gap_w5_w3_eod") is not None]
        row: dict[str, float] = {}
        if g20:
            row["gap_w20_w5_eod"] = mean(g20)
        if g53:
            row["gap_w5_w3_eod"] = mean(g53)
        out[sid] = row
    return out


def attach_within_between_components(
    legs: list[dict[str, Any]],
    *,
    stock_means: dict[str, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    """Add within / between gap components per leg."""
    if stock_means is None:
        stock_means = _stock_event_means(legs)
    out: list[dict[str, Any]] = []
    for leg in legs:
        leg = dict(leg)
        sid = str(leg.get("stock_id"))
        sm = stock_means.get(sid) or {}
        g20 = leg.get("gap_w20_w5_raw")
        g53 = leg.get("gap_w5_w3_raw")
        m20 = sm.get("gap_w20_w5")
        m53 = sm.get("gap_w5_w3")
        if g20 is not None and m20 is not None:
            leg["gap_w20_w5_within"] = round(float(g20) - m20, 4)
            leg["gap_w20_w5_between"] = round(m20, 4)
        if g53 is not None and m53 is not None:
            leg["gap_w5_w3_within"] = round(float(g53) - m53, 4)
            leg["gap_w5_w3_between"] = round(m53, 4)
        out.append(leg)
    return out


def gap_variance_decomposition(
    legs: list[dict[str, Any]],
    *,
    col: str = "gap_w20_w5_raw",
) -> dict[str, Any]:
    """ICC-style split: between-stock vs within-stock variance share."""
    pairs = [(str(leg["stock_id"]), float(leg[col])) for leg in legs if leg.get(col) is not None]
    if len(pairs) < 3:
        return {"col": col, "n": len(pairs), "skipped": True}
    grand_mean = mean(v for _, v in pairs)
    by_stock: dict[str, list[float]] = defaultdict(list)
    for sid, v in pairs:
        by_stock[sid].append(v)
    stock_means = {sid: mean(vs) for sid, vs in by_stock.items()}
    n = len(pairs)
    k = len(stock_means)
    ss_total = sum((v - grand_mean) ** 2 for _, v in pairs)
    ss_between = sum(len(vs) * (stock_means[sid] - grand_mean) ** 2 for sid, vs in by_stock.items())
    ss_within = sum((v - stock_means[sid]) ** 2 for sid, v in pairs)
    if ss_total < 1e-12:
        return {"col": col, "n": n, "k_stocks": k, "skipped": True}
    return {
        "col": col,
        "n": n,
        "k_stocks": k,
        "skipped": False,
        "var_total": round(ss_total / n, 4),
        "var_between": round(ss_between / n, 4),
        "var_within": round(ss_within / n, 4),
        "icc_between_share": round(ss_between / ss_total, 3),
        "icc_within_share": round(ss_within / ss_total, 3),
    }


def build_stock_level_rows(
    legs: list[dict[str, Any]],
    baseline_gap_means: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    """One row per stock for cross-sectional scan."""
    stock_means = _stock_event_means(legs)
    rows: list[dict[str, Any]] = []
    for sid, sm in stock_means.items():
        if sm.get("n_events", 0) < 1:
            continue
        bg = baseline_gap_means.get(sid) or {}
        row: dict[str, Any] = {
            "stock_id": sid,
            "n_events": int(sm["n_events"]),
            "return_pct": sm.get("return_pct"),
            "stock_mean_gap_w20_w5": sm.get("gap_w20_w5"),
            "stock_mean_gap_w5_w3": sm.get("gap_w5_w3"),
            "stock_mean_gap_w20_w5_eod": bg.get("gap_w20_w5_eod"),
            "stock_mean_gap_w5_w3_eod": bg.get("gap_w5_w3_eod"),
        }
        rows.append(row)
    return rows


def per_stock_within_correlations(
    legs: list[dict[str, Any]],
    *,
    factor_col: str = "gap_w20_w5_within",
    target: str = "return_pct",
    min_events: int = 3,
) -> dict[str, Any]:
    """Meta-analyze within-stock Spearman ρ (event-level deviations)."""
    from research.backtest.abc_v3_f1_entry_factor_scan import _spearman

    by_stock: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for leg in legs:
        if leg.get(factor_col) is not None and leg.get(target) is not None:
            by_stock[str(leg["stock_id"])].append(leg)

    stock_rhos: list[dict[str, Any]] = []
    for sid, sl in by_stock.items():
        if len(sl) < min_events:
            continue
        xs = [float(l[factor_col]) for l in sl]
        ys = [float(l[target]) for l in sl]
        rho = _spearman(xs, ys)
        if rho is not None:
            stock_rhos.append({"stock_id": sid, "n": len(sl), "spearman_rho": rho})

    if not stock_rhos:
        return {
            "factor": factor_col,
            "min_events": min_events,
            "n_stocks": 0,
            "skipped": True,
        }
    rhos = [float(r["spearman_rho"]) for r in stock_rhos]
    return {
        "factor": factor_col,
        "min_events": min_events,
        "n_stocks": len(stock_rhos),
        "skipped": False,
        "mean_rho": round(mean(rhos), 4),
        "median_rho": round(median(rhos), 4),
        "pct_positive": round(100.0 * sum(1 for r in rhos if r > 0) / len(rhos), 1),
        "stock_rhos": stock_rhos[:20],
    }


def _tercile_labels(values: dict[str, float]) -> dict[str, str]:
    """Map stock_id → low|mid|high by value terciles."""
    items = sorted(values.items(), key=lambda x: x[1])
    n = len(items)
    if n < 3:
        return {sid: "mid" for sid, _ in items}
    t1 = n // 3
    t2 = 2 * n // 3
    out: dict[str, str] = {}
    for i, (sid, _) in enumerate(items):
        if i < t1:
            out[sid] = "low"
        elif i < t2:
            out[sid] = "mid"
        else:
            out[sid] = "high"
    return out


def stratified_factor_scan_by_stock_gap(
    legs: list[dict[str, Any]],
    baseline_gap_means: dict[str, dict[str, float]],
    *,
    stratify_key: str = "gap_w20_w5_eod",
    factor_cols: tuple[str, ...] = FACTOR_COLUMNS,
    min_n: int = 15,
) -> list[dict[str, Any]]:
    """Run pooled factor scan within stock terciles (by historical EOD gap)."""
    values = {
        sid: float(bg[stratify_key])
        for sid, bg in baseline_gap_means.items()
        if bg.get(stratify_key) is not None
    }
    labels = _tercile_labels(values)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for leg in legs:
        sid = str(leg.get("stock_id"))
        if sid in labels:
            groups[labels[sid]].append(leg)

    rows: list[dict[str, Any]] = []
    for tier in ("low", "mid", "high"):
        tier_legs = groups.get(tier) or []
        scan = scan_factor_correlations(tier_legs, factor_cols=factor_cols, min_n=min_n)
        top = next((r for r in scan if not r.get("skipped")), None)
        rows.append(
            {
                "tier": tier,
                "stratify_key": stratify_key,
                "n_legs": len(tier_legs),
                "n_stocks": len({str(l["stock_id"]) for l in tier_legs}),
                "best_factor": top.get("factor") if top else None,
                "best_spearman": top.get("spearman_rho") if top else None,
                "best_lift_pp": top.get("tercile_lift") if top else None,
                "factor_scan": scan,
            }
        )
    return rows


def run_gap_decomposition_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-09-01",
    date_end: str = "2026-07-01",
    hold_days: int = 5,
    baseline_window: int = 60,
    min_trailing_days: int = 10,
    min_factor_n: int = 20,
    min_stock_n: int = 15,
    min_per_stock_events: int = 3,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    sample_dates = [d for d in full_dates if date_start <= d <= date_end]
    if not sample_dates:
        raise ValueError(f"no trading dates in [{date_start}, {date_end}]")

    legs = collect_abc_v3_f1_raw_legs(conn, dates=sample_dates, hold_days=hold_days, etf_codes=etf_codes)
    if not legs:
        raise ValueError("no raw ABC-V3-F1 legs in window")

    stock_ids = sorted({str(leg["stock_id"]) for leg in legs})
    earliest_entry = min(str(leg["entry_date"]) for leg in legs)
    try:
        entry_idx = full_dates.index(earliest_entry)
    except ValueError:
        entry_idx = 0
    lookback_start = max(0, entry_idx - baseline_window - 5)
    end_idx = full_dates.index(date_end) if date_end in full_dates else len(full_dates) - 1
    baseline_dates = full_dates[lookback_start : end_idx + 1]

    baseline = build_stock_daily_gap_rv_series(
        conn, stock_ids=stock_ids, dates=baseline_dates, etf_codes=etf_codes
    )
    legs = attach_normalized_gate_features(
        legs, baseline, window=baseline_window, min_trailing=min_trailing_days
    )
    legs = attach_within_between_components(legs)
    baseline_gap_means = _stock_baseline_gap_means(baseline, before_date=date_end, window=baseline_window)

    pooled_raw = scan_factor_correlations(legs, factor_cols=FACTOR_COLUMNS, min_n=min_factor_n)
    pooled_within_between = scan_factor_correlations(
        legs, factor_cols=WITHIN_BETWEEN_COLS, min_n=min_factor_n
    )
    stock_rows = build_stock_level_rows(legs, baseline_gap_means)
    cross_section = scan_factor_correlations(
        stock_rows, factor_cols=STOCK_LEVEL_COLS, target="return_pct", min_n=min_stock_n
    )

    variance = [
        gap_variance_decomposition(legs, col="gap_w20_w5_raw"),
        gap_variance_decomposition(legs, col="gap_w5_w3_raw"),
    ]

    within_meta = [
        per_stock_within_correlations(
            legs, factor_col="gap_w20_w5_within", min_events=min_per_stock_events
        ),
        per_stock_within_correlations(
            legs, factor_col="gap_w5_w3_within", min_events=min_per_stock_events
        ),
        per_stock_within_correlations(
            legs, factor_col="gap_w20_w5_z", min_events=min_per_stock_events
        ),
    ]

    stratified = stratified_factor_scan_by_stock_gap(
        legs, baseline_gap_means, stratify_key="gap_w20_w5_eod", min_n=min_factor_n
    )
    stratified_w53 = stratified_factor_scan_by_stock_gap(
        legs, baseline_gap_means, stratify_key="gap_w5_w3_eod", min_n=min_factor_n
    )

    return {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": date_end,
        "hold_days": hold_days,
        "n_legs": len(legs),
        "n_stocks": len(stock_ids),
        "n_stock_level_rows": len(stock_rows),
        "pooled_factor_scan": pooled_raw,
        "within_between_scan": pooled_within_between,
        "cross_section_stock_scan": cross_section,
        "variance_decomposition": variance,
        "per_stock_within_meta": within_meta,
        "stratified_by_gap_w20_w5_eod": stratified,
        "stratified_by_gap_w5_w3_eod": stratified_w53,
    }


def render_gap_decomposition_md(payload: dict[str, Any]) -> str:
    lines = [
        "# ABC v3+F1 · gap 截面 vs 时序分解",
        "",
        f"**{payload.get('n_legs')}** legs · **{payload.get('n_stocks')}** 檔股票 · "
        f"窗口 **{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"hold **{payload.get('hold_days')}d**",
        "",
        "## 1. 變異數分解（ICC）",
        "",
        "gap 變異有多少來自「股票之間」vs「同一股票不同事件」：",
        "",
        "| 因子 | n | 股票數 | Between share | Within share |",
        "|------|--:|-------:|--------------:|-------------:|",
    ]
    for v in payload.get("variance_decomposition") or []:
        if v.get("skipped"):
            lines.append(f"| {v.get('col')} | {v.get('n')} | — | — | — |")
        else:
            lines.append(
                f"| {v.get('col')} | {v.get('n')} | {v.get('k_stocks')} | "
                f"**{v.get('icc_between_share')}** | {v.get('icc_within_share')} |"
            )

    lines.extend(
        [
            "",
            "## 2. 截面（between-stock）· 股票層級",
            "",
            "每檔股票在樣本內的平均 gap vs 平均 hold 回报（n = 股票數）：",
            "",
            "| Factor | n | Spearman ρ | Lift(pp) |",
            "|--------|--:|-----------:|---------:|",
        ]
    )
    for row in payload.get("cross_section_stock_scan") or []:
        if row.get("skipped"):
            lines.append(f"| {row.get('factor')} | {row.get('n')} | — | — |")
        else:
            lines.append(
                f"| {row.get('factor')} | {row.get('n')} | {row.get('spearman_rho')} | "
                f"{row.get('tercile_lift')} |"
            )

    lines.extend(
        [
            "",
            "## 3. 时序（within-stock）· 事件層級",
            "",
            "每筆 leg 的 gap 減去該股票樣本內均值（within 偏差）vs 回报：",
            "",
            "| Component | n | Spearman ρ | Lift(pp) |",
            "|-----------|--:|-----------:|---------:|",
        ]
    )
    for row in payload.get("within_between_scan") or []:
        if row.get("skipped"):
            continue
        if str(row.get("factor", "")).endswith("_within"):
            lines.append(
                f"| {row.get('factor')} | {row.get('n')} | {row.get('spearman_rho')} | "
                f"{row.get('tercile_lift')} |"
            )

    lines.extend(["", "### Per-stock 时序相关 meta 汇总", ""])
    for m in payload.get("per_stock_within_meta") or []:
        if m.get("skipped"):
            lines.append(f"- `{m.get('factor')}`：樣本不足")
        else:
            lines.append(
                f"- `{m.get('factor')}`：{m.get('n_stocks')} 檔（≥{m.get('min_events')} events）· "
                f"mean ρ={m.get('mean_rho')} · median ρ={m.get('median_rho')} · "
                f"正相關占比 {m.get('pct_positive')}%"
            )

    lines.extend(
        [
            "",
            "## 4. 按股票历史 gap 振幅分群 · 群内 factor scan",
            "",
            "依 trailing EOD `gap_w20_w5` 把股票分 low/mid/high 三群，在群内重跑 12 因子 scan：",
            "",
            "| Tier | n legs | n stocks | Best factor | |ρ| | Lift(pp) |",
            "|------|-------:|---------:|-------------|----:|---------:|",
        ]
    )
    for block in payload.get("stratified_by_gap_w20_w5_eod") or []:
        lines.append(
            f"| {block.get('tier')} | {block.get('n_legs')} | {block.get('n_stocks')} | "
            f"`{block.get('best_factor')}` | {abs(float(block.get('best_spearman') or 0))} | "
            f"{block.get('best_lift_pp')} |"
        )

    lines.extend(
        [
            "",
            "## 5. 解讀指引",
            "",
            "- **ICC between 高**：固定 gap 門檻主要在「選股」（截面），不是「擇時」",
            "- **within 相關為負、between 為正**：與 pooled raw 方向翻轉一致 → sweet-band 抓的是股票類型",
            "- **分群後 |ρ| 仍弱**：即使在同類股票內，單因子預測力仍不足",
            "",
            "模組：`scripts/research/run_abc_v3_f1_entry_gap_decomposition.py`",
        ]
    )
    return "\n".join(lines)
