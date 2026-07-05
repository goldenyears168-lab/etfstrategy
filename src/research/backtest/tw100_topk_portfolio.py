"""TW100 Top-K equal-weight portfolio · Research layer (Alpha158/LGBM WFA OOS)."""

from __future__ import annotations

import math
import sqlite3
from typing import Any

import pandas as pd

from research.backtest.tw100_ml_backtest import forward_label, load_close_panel


def daily_topk_portfolio_from_panels(
    pred: pd.Series,
    label: pd.Series,
    *,
    k: int,
    min_cross_section: int = 30,
) -> list[dict[str, Any]]:
    """Equal-weight long top-K by prediction · mean forward label per signal day."""
    df = pd.concat([pred.rename("pred"), label.rename("label")], axis=1)
    df["label"] = df["label"].replace([float("inf"), float("-inf")], pd.NA)
    df["label"] = df["label"].clip(lower=-0.5, upper=0.5)
    rows: list[dict[str, Any]] = []
    prev_top: frozenset[str] | None = None
    for dt, grp in df.groupby(level="datetime"):
        if len(grp) < min_cross_section:
            continue
        scored = grp.dropna(subset=["pred", "label"])
        if len(scored) < k:
            continue
        top = scored.nlargest(k, "pred")
        instruments = frozenset(str(x) for x in top.index.get_level_values("instrument"))
        turnover = 0.0 if prev_top is None else 1.0 - len(instruments & prev_top) / float(k)
        prev_top = instruments
        rows.append(
            {
                "date": pd.Timestamp(dt).strftime("%Y-%m-%d"),
                "ret": round(float(top["label"].mean()), 8),
                "turnover": round(float(turnover), 6),
                "n": k,
            }
        )
    return rows


def raw_forward_label_for_pred(
    conn: sqlite3.Connection,
    pred: pd.Series,
) -> pd.Series:
    """Actual T+1→T+3 forward returns aligned to pred MultiIndex (not CSZScoreNorm)."""
    instruments = sorted({str(x) for x in pred.index.get_level_values("instrument")})
    dates = sorted({pd.Timestamp(d).strftime("%Y-%m-%d") for d in pred.index.get_level_values("datetime")})
    if not instruments or not dates:
        return pd.Series(dtype=float)
    from datetime import datetime, timedelta

    end_buf = (datetime.strptime(dates[-1], "%Y-%m-%d").date() + timedelta(days=14)).isoformat()
    panel = load_close_panel(conn, instruments, start=dates[0], end=end_buf)
    if panel.empty:
        return pd.Series(dtype=float)
    label_panel = forward_label(panel)
    melted = label_panel.stack(future_stack=True).rename("label")
    melted.index.names = ["datetime", "instrument"]
    melted.index = melted.index.set_levels(
        [pd.to_datetime(melted.index.levels[0]), melted.index.levels[1]],
        level=[0, 1],
    )
    pred_index = pred.index
    if isinstance(pred_index, pd.MultiIndex):
        dt_level = pd.to_datetime(pred_index.get_level_values("datetime"))
        inst_level = pred_index.get_level_values("instrument").astype(str)
        pred_index = pd.MultiIndex.from_arrays([dt_level, inst_level], names=["datetime", "instrument"])
    return melted.reindex(pred_index)


def load_benchmark_forward_returns(
    conn: sqlite3.Connection,
    benchmark_id: str,
    *,
    start: str,
    end: str,
) -> dict[str, float]:
    panel = load_close_panel(conn, [benchmark_id], start=start, end=end)
    if panel.empty or benchmark_id not in panel.columns:
        return {}
    label = forward_label(panel[[benchmark_id]], horizon=2)
    out: dict[str, float] = {}
    for dt in label.index:
        val = label.at[dt, benchmark_id]
        if pd.notna(val):
            out[str(dt)] = float(val)
    return out


def load_index_benchmark_forward_returns(
    conn: sqlite3.Connection,
    *,
    start: str,
    end: str,
    code: str = "IX0001",
) -> dict[str, float]:
    from market_benchmark import load_benchmark_close

    close = load_benchmark_close(conn, code=code)
    close = close[(close.index >= start) & (close.index <= end)]
    if close.empty:
        return {}
    panel = close.to_frame(name=code)
    label = forward_label(panel, horizon=2)
    return {str(dt): float(label.at[dt, code]) for dt in label.index if pd.notna(label.at[dt, code])}


def _max_drawdown(cumulative: list[float]) -> float | None:
    if not cumulative:
        return None
    peak = cumulative[0]
    max_dd = 0.0
    for v in cumulative:
        peak = max(peak, v)
        dd = (v - peak) / peak if peak > 0 else 0.0
        max_dd = min(max_dd, dd)
    return round(max_dd, 6)


def summarize_portfolio_daily(
    daily_rows: list[dict[str, Any]],
    bench_by_date: dict[str, float],
    *,
    cost_bps: float = 0.0,
    periods_per_year: float = 126.0,
) -> dict[str, Any]:
    """126 ≈ 252/2 · label is ~2-day forward return per signal day."""
    if not daily_rows:
        return {"n_days": 0}

    net_rets: list[float] = []
    gross_rets: list[float] = []
    excess: list[float] = []
    turnovers: list[float] = []
    cum = 1.0
    cum_curve: list[float] = [1.0]

    for row in daily_rows:
        gross = float(row["ret"])
        turnover = float(row.get("turnover") or 0.0)
        cost = turnover * cost_bps / 10_000.0
        net = gross - cost
        if not math.isfinite(net):
            continue
        gross_rets.append(gross)
        net_rets.append(net)
        turnovers.append(turnover)
        cum *= 1.0 + net
        cum_curve.append(cum)
        bench = bench_by_date.get(str(row["date"]))
        if bench is not None:
            excess.append(net - bench)

    n = len(net_rets)
    mean_net = sum(net_rets) / n
    std_net = math.sqrt(sum((r - mean_net) ** 2 for r in net_rets) / n) if n > 1 else 0.0
    sharpe = (mean_net / std_net * math.sqrt(periods_per_year)) if std_net > 0 else None

    mean_excess = sum(excess) / len(excess) if excess else None
    std_excess = (
        math.sqrt(sum((e - mean_excess) ** 2 for e in excess) / len(excess))
        if excess and len(excess) > 1 and mean_excess is not None
        else 0.0
    )
    excess_sharpe = (
        (mean_excess / std_excess * math.sqrt(periods_per_year))
        if excess and std_excess > 0 and mean_excess is not None
        else None
    )

    bench_cum = 1.0
    for row in daily_rows:
        b = bench_by_date.get(str(row["date"]))
        if b is not None:
            bench_cum *= 1.0 + b

    return {
        "n_days": n,
        "total_return": round(cum - 1.0, 6),
        "mean_return": round(mean_net, 6),
        "mean_gross_return": round(sum(gross_rets) / n, 6),
        "volatility": round(std_net * math.sqrt(periods_per_year), 6) if std_net else None,
        "sharpe": round(sharpe, 6) if sharpe is not None else None,
        "max_drawdown": _max_drawdown(cum_curve),
        "mean_turnover": round(sum(turnovers) / n, 6),
        "cost_bps": cost_bps,
        "benchmark_id": None,
        "benchmark_total_return": round(bench_cum - 1.0, 6) if bench_by_date else None,
        "mean_excess_vs_benchmark": round(mean_excess, 6) if mean_excess is not None else None,
        "excess_sharpe": round(excess_sharpe, 6) if excess_sharpe is not None else None,
        "excess_n_days": len(excess),
    }


def aggregate_oos_portfolio(
    fold_daily: list[list[dict[str, Any]]],
    bench_by_date: dict[str, float],
    *,
    cost_bps: float = 0.0,
    benchmark_id: str = "0050",
) -> dict[str, Any]:
    flat = [row for fold in fold_daily for row in fold]
    flat.sort(key=lambda r: r["date"])
    summary = summarize_portfolio_daily(flat, bench_by_date, cost_bps=cost_bps)
    summary["benchmark_id"] = benchmark_id
    return summary
