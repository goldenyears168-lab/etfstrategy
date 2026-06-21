"""Benchmark (IX0001) price lookup and period returns · shared by tracks and research."""

from __future__ import annotations

import sqlite3
from typing import Protocol

from flow_returns import BENCHMARK_CODE, return_pct


class ExcessReturnRow(Protocol):
    status: str
    return_pct: float
    bench_return_pct: float


def bench_close(conn: sqlite3.Connection, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM daily_bars
        WHERE code = ? AND date = ?
        ORDER BY CASE source WHEN 'tej' THEN 0 WHEN 'yahoo' THEN 1 ELSE 2 END
        LIMIT 1
        """,
        (BENCHMARK_CODE, trade_date),
    ).fetchone()
    if row is None:
        return None
    return float(row["close"])


def bench_open(conn: sqlite3.Connection, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT open, close FROM daily_bars
        WHERE code = ? AND date = ?
        ORDER BY CASE source WHEN 'tej' THEN 0 WHEN 'yahoo' THEN 1 ELSE 2 END
        LIMIT 1
        """,
        (BENCHMARK_CODE, trade_date),
    ).fetchone()
    if row is None:
        return None
    if row["open"] is not None and float(row["open"]) > 0:
        return float(row["open"])
    if row["close"] is not None:
        return float(row["close"])
    return None


def bench_return_entry_to_exit(
    conn: sqlite3.Connection,
    entry_date: str,
    exit_date: str,
    *,
    entry_price_mode: str,
) -> float | None:
    if entry_price_mode == "close":
        b0 = bench_close(conn, entry_date)
    else:
        b0 = bench_open(conn, entry_date)
    b1 = bench_close(conn, exit_date)
    if b0 is None or b1 is None:
        return None
    return return_pct(b0, b1)


def compute_excess_significance(
    day_results: list[ExcessReturnRow],
) -> dict[str, float | None]:
    """每日超額報酬 (return − bench) 對 0 的檢定。"""
    complete = [d for d in day_results if d.status == "complete"]
    if not complete:
        return {
            "mean_excess_pct": None,
            "p_value_ttest": None,
            "p_value_wilcoxon": None,
            "t_stat": None,
        }
    excess = [d.return_pct - d.bench_return_pct for d in complete]
    mean_ex = sum(excess) / len(excess)
    if len(complete) < 3:
        return {
            "mean_excess_pct": round(mean_ex, 4),
            "p_value_ttest": None,
            "p_value_wilcoxon": None,
            "t_stat": None,
        }
    try:
        from scipy.stats import ttest_1samp, wilcoxon

        t_stat, p_t = ttest_1samp(excess, 0.0)
        non_zero = [e for e in excess if abs(e) > 1e-12]
        if len(non_zero) >= 3:
            _, p_w = wilcoxon(non_zero)
        else:
            p_w = None
    except Exception:
        return {
            "mean_excess_pct": round(mean_ex, 4),
            "p_value_ttest": None,
            "p_value_wilcoxon": None,
            "t_stat": None,
        }
    return {
        "mean_excess_pct": round(mean_ex, 4),
        "p_value_ttest": round(float(p_t), 4) if p_t == p_t else None,
        "p_value_wilcoxon": round(float(p_w), 4) if p_w is not None and p_w == p_w else None,
        "t_stat": round(float(t_stat), 4) if t_stat == t_stat else None,
    }
