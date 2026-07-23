"""ABC v3+F1 · entry factor correlation scan · per-stock normalized variants.

Motivation: a single fixed absolute gap/rebound threshold assumes every stock
oscillates on the same scale. This module tests that assumption directly by
computing, for each raw gate feature (MV gap W20-W5, MV gap W5-W3, W3 RV
trough rebound), four variants:

  - raw:    the literal value (what the fixed-threshold gate already uses)
  - z:      z-score against that stock's OWN trailing daily history (no
            lookahead — only days strictly before entry_date)
  - pct:    percentile rank against that stock's own trailing daily history
  - ratio:  a ratio-based redefinition that needs no history at all
            (MV20/MV5 instead of MV20-MV5; rebound as % of the day's own
            trough instead of an absolute point difference)

...then scans correlation (Pearson, Spearman, tercile lift) of each variant
against forward hold_days return, across the raw (unconstrained) ABC-V3-F1
event pool. Purpose: broad, low-commitment observation of which
normalization — if any — carries more signal than the fixed-threshold raw
version, before committing to a specific gate design.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from project_config import DEFAULT_ETF_CODES
from research.backtest.abc_v3_f1_entry_gate_tp_sweep import collect_abc_v3_f1_raw_legs
from research.backtest.abc_v3_f1_entry_structure_sweep import Rv3TroughState, _safe_gap
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.triple_wma_pullback_sweep import _mv, _rv
from research.backtest.w3_rv_hl_winner_profile import _poll_idx_for_frame
from rrg_universe_intraday_panel import (
    WMA_MICRO_LENGTH,
    WMA_ULTRA_LENGTH,
    build_dual_wma_intraday_trajectories,
)

FACTOR_COLUMNS: tuple[str, ...] = (
    "gap_w20_w5_raw",
    "gap_w20_w5_z",
    "gap_w20_w5_pct",
    "gap_w20_w5_ratio",
    "gap_w5_w3_raw",
    "gap_w5_w3_z",
    "gap_w5_w3_pct",
    "gap_w5_w3_ratio",
    "rv3_rebound_raw",
    "rv3_rebound_z",
    "rv3_rebound_pct",
    "rv3_rebound_ratio",
)


def build_stock_daily_gap_rv_series(
    conn: sqlite3.Connection,
    *,
    stock_ids: list[str],
    dates: list[str],
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, list[dict[str, Any]]]:
    """Per-stock daily baseline series: EOD (13:30) MV gaps + the day's own
    max W3 RV trough rebound. Used as each stock's own trailing history for
    z-score / percentile normalization — not compared across stocks."""
    frames, trajectories, _ = build_dual_wma_intraday_trajectories(
        conn,
        dates=dates,
        stock_ids=stock_ids,
        etf_codes=etf_codes,
        micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
    )
    frame_ids = [f["id"] for f in frames]
    out: dict[str, list[dict[str, Any]]] = {}
    for t in trajectories:
        sid = t["stock_id"]
        by_id = {p["frame_id"]: p for p in t["points"]}
        trough = Rv3TroughState()
        rows: list[dict[str, Any]] = []
        current_date: str | None = None
        day_gap20_5: float | None = None
        day_gap5_3: float | None = None
        day_max_rebound: float | None = None

        def _flush() -> None:
            if current_date is not None and (
                day_gap20_5 is not None or day_gap5_3 is not None or day_max_rebound is not None
            ):
                rows.append(
                    {
                        "date": current_date,
                        "gap_w20_w5_eod": day_gap20_5,
                        "gap_w5_w3_eod": day_gap5_3,
                        "max_rv3_rebound_day": day_max_rebound,
                    }
                )

        for i, fid in enumerate(frame_ids):
            poll_idx = _poll_idx_for_frame(frames, i)
            if poll_idx is None:
                continue
            d = frames[i]["date"]
            if current_date is not None and d != current_date:
                _flush()
                day_gap20_5 = day_gap5_3 = day_max_rebound = None
            current_date = d
            p = by_id.get(fid)
            if p is None:
                continue
            if poll_idx == 0:
                trough.reset()
            rv3 = _rv(p, "w3")
            if rv3 is not None:
                rebound, _ = trough.update(rv3)
                day_max_rebound = rebound if day_max_rebound is None else max(day_max_rebound, rebound)
            g20_5 = _safe_gap(_mv(p, "w20"), _mv(p, "w5"))
            g5_3 = _safe_gap(_mv(p, "w5"), _mv(p, "w3"))
            if g20_5 is not None:
                day_gap20_5 = g20_5
            if g5_3 is not None:
                day_gap5_3 = g5_3
        _flush()
        out[sid] = rows
    return out


def _trailing_window(
    series: list[dict[str, Any]],
    *,
    before_date: str,
    window: int,
    key: str,
) -> list[float]:
    """Values strictly before ``before_date`` (no lookahead), most recent ``window``."""
    vals = [r[key] for r in series if r["date"] < before_date and r.get(key) is not None]
    return vals[-window:] if len(vals) > window else vals


def _zscore(raw: float, trailing: list[float]) -> float | None:
    n = len(trailing)
    if n < 2:
        return None
    mean = sum(trailing) / n
    var = sum((x - mean) ** 2 for x in trailing) / n
    std = var**0.5
    if std < 1e-9:
        return None
    return round((raw - mean) / std, 4)


def _percentile(raw: float, trailing: list[float]) -> float | None:
    n = len(trailing)
    if n < 1:
        return None
    n_le = sum(1 for x in trailing if x <= raw)
    return round(100.0 * n_le / n, 2)


def attach_normalized_gate_features(
    legs: list[dict[str, Any]],
    baseline: dict[str, list[dict[str, Any]]],
    *,
    window: int = 60,
    min_trailing: int = 10,
) -> list[dict[str, Any]]:
    """Add raw/z/percentile/ratio variants of gap_w20_w5, gap_w5_w3, rv3_rebound
    onto each leg (from collect_abc_v3_f1_raw_legs), keyed to FACTOR_COLUMNS."""
    out: list[dict[str, Any]] = []
    for leg in legs:
        leg = dict(leg)
        sid = str(leg.get("stock_id"))
        entry_date = str(leg.get("entry_date"))
        series = baseline.get(sid) or []

        raw_gap20_5 = leg.get("gate_gap_w20_w5")
        raw_gap5_3 = leg.get("gate_gap_w5_w3")
        raw_rebound = leg.get("gate_rv3_rebound")

        trailing20_5 = _trailing_window(series, before_date=entry_date, window=window, key="gap_w20_w5_eod")
        trailing5_3 = _trailing_window(series, before_date=entry_date, window=window, key="gap_w5_w3_eod")
        trailing_reb = _trailing_window(series, before_date=entry_date, window=window, key="max_rv3_rebound_day")

        leg["gap_w20_w5_raw"] = raw_gap20_5
        leg["gap_w20_w5_z"] = (
            _zscore(raw_gap20_5, trailing20_5)
            if raw_gap20_5 is not None and len(trailing20_5) >= min_trailing
            else None
        )
        leg["gap_w20_w5_pct"] = (
            _percentile(raw_gap20_5, trailing20_5)
            if raw_gap20_5 is not None and len(trailing20_5) >= min_trailing
            else None
        )

        leg["gap_w5_w3_raw"] = raw_gap5_3
        leg["gap_w5_w3_z"] = (
            _zscore(raw_gap5_3, trailing5_3)
            if raw_gap5_3 is not None and len(trailing5_3) >= min_trailing
            else None
        )
        leg["gap_w5_w3_pct"] = (
            _percentile(raw_gap5_3, trailing5_3)
            if raw_gap5_3 is not None and len(trailing5_3) >= min_trailing
            else None
        )

        leg["rv3_rebound_raw"] = raw_rebound
        leg["rv3_rebound_z"] = (
            _zscore(raw_rebound, trailing_reb)
            if raw_rebound is not None and len(trailing_reb) >= min_trailing
            else None
        )
        leg["rv3_rebound_pct"] = (
            _percentile(raw_rebound, trailing_reb)
            if raw_rebound is not None and len(trailing_reb) >= min_trailing
            else None
        )

        entry_t0 = leg.get("entry_t0") or {}
        mv20, mv5, mv3 = entry_t0.get("w20_mv"), entry_t0.get("w5_mv"), entry_t0.get("w3_mv")
        rv3_now = entry_t0.get("w3_rv")
        leg["gap_w20_w5_ratio"] = (
            round((float(mv20) / float(mv5) - 1.0) * 100, 4)
            if mv20 is not None and mv5 not in (None, 0)
            else None
        )
        leg["gap_w5_w3_ratio"] = (
            round((float(mv5) / float(mv3) - 1.0) * 100, 4) if mv5 is not None and mv3 not in (None, 0) else None
        )
        if raw_rebound is not None and rv3_now is not None:
            trough = float(rv3_now) - float(raw_rebound)
            leg["rv3_rebound_ratio"] = round(float(raw_rebound) / trough * 100, 4) if abs(trough) > 1e-6 else None
        else:
            leg["rv3_rebound_ratio"] = None
        out.append(leg)
    return out


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx < 1e-12 or vy < 1e-12:
        return None
    return round(cov / (vx * vy) ** 0.5, 4)


def _rank(vals: list[float]) -> list[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    return _pearson(_rank(xs), _rank(ys))


def scan_factor_correlations(
    legs: list[dict[str, Any]],
    *,
    factor_cols: tuple[str, ...] = FACTOR_COLUMNS,
    target: str = "return_pct",
    min_n: int = 20,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for col in factor_cols:
        pairs = [(leg[col], float(leg[target])) for leg in legs if leg.get(col) is not None]
        n = len(pairs)
        if n < min_n:
            rows.append({"factor": col, "n": n, "skipped": True})
            continue
        xs = [float(p[0]) for p in pairs]
        ys = [p[1] for p in pairs]
        sorted_pairs = sorted(zip(xs, ys), key=lambda p: p[0])
        k = max(1, n // 3)
        bottom = [p[1] for p in sorted_pairs[:k]]
        top = [p[1] for p in sorted_pairs[-k:]]
        rows.append(
            {
                "factor": col,
                "n": n,
                "skipped": False,
                "pearson_r": _pearson(xs, ys),
                "spearman_rho": _spearman(xs, ys),
                "top_tercile_mean": round(sum(top) / len(top), 3),
                "bottom_tercile_mean": round(sum(bottom) / len(bottom), 3),
                "tercile_lift": round(sum(top) / len(top) - sum(bottom) / len(bottom), 3),
            }
        )
    rows.sort(key=lambda r: abs(float(r.get("spearman_rho") or 0)), reverse=True)
    return rows


def run_entry_factor_scan_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-09-01",
    date_end: str = "2026-07-01",
    hold_days: int = 5,
    baseline_window: int = 60,
    min_trailing_days: int = 10,
    min_factor_n: int = 20,
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
    scan = scan_factor_correlations(legs, min_n=min_factor_n)

    return {
        "schema": "abc_v3_f1_entry_factor_scan-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": date_end,
        "hold_days": hold_days,
        "baseline_window": baseline_window,
        "min_trailing_days": min_trailing_days,
        "n_legs": len(legs),
        "n_stocks": len(stock_ids),
        "factor_scan": scan,
    }


def render_entry_factor_scan_md(payload: dict[str, Any]) -> str:
    scan = payload.get("factor_scan") or []
    lines = [
        "# ABC v3+F1 · entry 因子相關性掃描（raw / z-score / percentile / ratio）",
        "",
        f"**{payload.get('n_legs')}** legs · **{payload.get('n_stocks')}** 檔股票 · "
        f"窗口 **{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"hold **{payload.get('hold_days')}d** · baseline window **{payload.get('baseline_window')}** 天",
        "",
        "## 因子定義",
        "",
        "- `_raw`：原始絕對值（固定門檻現行使用的版本）",
        "- `_z`：對該股票自身 trailing 歷史做 z-score（僅用進場日之前的資料，無 lookahead）",
        "- `_pct`：對該股票自身 trailing 歷史做百分位排名",
        "- `_ratio`：比值型重新定義（MV20/MV5 取代 MV20−MV5；rebound 相對於當日谷底的百分比，取代絕對差值），不需要歷史視窗",
        "",
        "## 與未來報酬的相關性（依 |Spearman ρ| 排序）",
        "",
        "| Factor | n | Pearson r | Spearman ρ | Top tercile 均% | Bottom tercile 均% | Lift(pp) |",
        "|--------|--:|----------:|-----------:|----------------:|--------------------:|---------:|",
    ]
    for row in scan:
        if row.get("skipped"):
            lines.append(f"| {row.get('factor')} | {row.get('n')} | — | — | — | — | — |")
            continue
        lines.append(
            f"| {row.get('factor')} | {row.get('n')} | {row.get('pearson_r')} | {row.get('spearman_rho')} | "
            f"{row.get('top_tercile_mean')} | {row.get('bottom_tercile_mean')} | {row.get('tercile_lift')} |"
        )
    top_valid = [r for r in scan if not r.get("skipped")]
    lines.extend(["", "## 觀察", ""])
    if top_valid:
        best = top_valid[0]
        lines.append(
            f"- 相關性最強：`{best.get('factor')}`（Spearman ρ={best.get('spearman_rho')}，"
            f"tercile lift={best.get('tercile_lift')}pp，n={best.get('n')}）"
        )
        raw_rows = {r["factor"]: r for r in top_valid if r["factor"].endswith("_raw")}
        for base in ("gap_w20_w5", "gap_w5_w3", "rv3_rebound"):
            variants = [r for r in top_valid if r["factor"].startswith(base)]
            if len(variants) < 2:
                continue
            variants_sorted = sorted(variants, key=lambda r: abs(float(r.get("spearman_rho") or 0)), reverse=True)
            winner = variants_sorted[0]
            raw = raw_rows.get(f"{base}_raw")
            if raw and winner["factor"] != raw["factor"]:
                lines.append(
                    f"- `{base}`：{winner['factor']} 比 raw 更相關"
                    f"（ρ {winner.get('spearman_rho')} vs {raw.get('spearman_rho')}）"
                )
    else:
        lines.append("- 樣本量不足以進行任何因子的相關性檢定")
    lines.extend(["", "模組：`scripts/research/run_abc_v3_f1_entry_factor_scan.py`"])
    return "\n".join(lines)
