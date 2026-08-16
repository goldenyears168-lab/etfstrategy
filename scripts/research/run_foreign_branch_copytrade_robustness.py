#!/usr/bin/env python3
"""外資分點 copytrade 主表的穩健性複查（成本敏感度 + 可成交性 + 標的畫像）。

吃 `run_foreign_branch_copytrade_compare.py` 產出的 JSON（density-matched 門檻），
對每支外資分點再問三件事：

1. **成本敏感度** — 同一組訊號在 cost_bps ∈ {0, 20, 45} 下 OOS 還剩多少（45bps ≈
   雙邊手續費折扣後 + 證交稅 0.3% 的實務往返成本）。
2. **可成交性** — T+1 開盤是否 open==high==low（一價鎖死、跟不到）或跳空 ≥ +9%。
3. **標的畫像** — 訊號標的的 20 日均量與市值代理（成交金額），看是大型股還是小型股。

  PYTHONPATH=src .venv/bin/python scripts/research/run_foreign_branch_copytrade_robustness.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from copytrade.branch_signals import iter_branch_amount_buy_signals  # noqa: E402
from report_paths import REPORTS_RESEARCH  # noqa: E402
from research.backtest.branch_copytrade_fair_compare import (  # noqa: E402
    evaluate_branch_window,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

COST_GRID_BPS = (0.0, 20.0, 45.0)


def _next_bar(conn: sqlite3.Connection, stock_id: str, after: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT trade_date, open, high, low, close, volume
        FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' AND trade_date > ?
          AND open IS NOT NULL AND open > 0
        ORDER BY trade_date LIMIT 1
        """,
        (stock_id, after),
    ).fetchone()


def _signal_bar(conn: sqlite3.Connection, stock_id: str, on: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT close, volume
        FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' AND trade_date = ?
        """,
        (stock_id, on),
    ).fetchone()


def fillability_profile(
    conn: sqlite3.Connection,
    trader_id: str,
    *,
    min_net_amount_ntd: float,
    top_n: int,
    window_start: str,
    window_end: str,
) -> dict:
    signals = iter_branch_amount_buy_signals(
        conn,
        trader_id,
        min_net_amount_ntd=min_net_amount_ntd,
        top_n=top_n,
        window_start=window_start,
        window_end=window_end,
    )
    n_locked = 0
    n_gap_up_9 = 0
    n_eval = 0
    gaps: list[float] = []
    turnovers: list[float] = []
    stock_hits: dict[str, int] = {}
    for s in signals:
        stock_hits[s.stock_id] = stock_hits.get(s.stock_id, 0) + 1
        sig_bar = _signal_bar(conn, s.stock_id, s.signal_date)
        nxt = _next_bar(conn, s.stock_id, s.signal_date)
        if sig_bar is None or nxt is None or not sig_bar["close"]:
            continue
        n_eval += 1
        prev_close = float(sig_bar["close"])
        if sig_bar["volume"]:
            turnovers.append(prev_close * float(sig_bar["volume"]) / 1e8)  # 億元
        op = float(nxt["open"])
        gap = 100.0 * (op / prev_close - 1.0)
        gaps.append(gap)
        if gap >= 9.0:
            n_gap_up_9 += 1
        hi, lo = nxt["high"], nxt["low"]
        if hi is not None and lo is not None and float(hi) == float(lo):
            n_locked += 1
    top_names = sorted(stock_hits.items(), key=lambda kv: -kv[1])[:8]
    return {
        "n_signals": len(signals),
        "n_evaluated": n_eval,
        "n_distinct_stocks": len(stock_hits),
        "pct_entry_bar_locked": round(100.0 * n_locked / n_eval, 2) if n_eval else None,
        "pct_entry_gap_ge_9pct": round(100.0 * n_gap_up_9 / n_eval, 2) if n_eval else None,
        "median_entry_gap_pct": round(statistics.median(gaps), 3) if gaps else None,
        "mean_entry_gap_pct": round(statistics.mean(gaps), 3) if gaps else None,
        "median_signal_turnover_100m_ntd": (
            round(statistics.median(turnovers), 2) if turnovers else None
        ),
        "top_repeat_stocks": [{"stock_id": k, "n": v} for k, v in top_names],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Foreign branch copytrade robustness")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--compare-json",
        type=Path,
        default=REPORTS_RESEARCH
        / "foreign-branch-copytrade"
        / f"{date.today():%Y%m%d}_foreign_branch_copytrade_compare.json",
    )
    ap.add_argument("--top", type=int, default=99, help="Only re-check top N by OOS")
    args = ap.parse_args()

    payload = json.loads(args.compare_json.read_text(encoding="utf-8"))
    proto = payload["protocol"]
    ranked = payload["rank_density_matched_oos"][: args.top]

    conn = connect(args.db)
    rows: list[dict] = []
    try:
        for r in ranked:
            spec = next(b for b in payload["branches"] if b["slug"] == r["slug"])
            amount_ntd = float(r["min_net_amount_m"]) * 1e6
            etf_proxy = f"BR_{r['slug'].upper().replace('-', '_')}"
            costs = {}
            for bps in COST_GRID_BPS:
                ev = evaluate_branch_window(
                    conn,
                    trader_id=spec["trader_id"],
                    etf_proxy=etf_proxy,
                    label_prefix=r["label"],
                    min_net_amount_ntd=amount_ntd,
                    top_n=proto["top_n"],
                    window_start=proto["oos_cut"],
                    window_end=proto["window_end"],
                    cost_bps=bps,
                )
                costs[f"cost_{bps:g}bps"] = {
                    "n_cycles": ev.get("n_cycles"),
                    "period_return_pct": ev.get("period_return_pct"),
                    "mean_return_pct": ev.get("mean_return_pct"),
                    "win_rate_pct": ev.get("win_rate_pct"),
                    "alpha_per_cycle": ev.get("alpha_per_cycle"),
                }
            fill = fillability_profile(
                conn,
                spec["trader_id"],
                min_net_amount_ntd=amount_ntd,
                top_n=proto["top_n"],
                window_start=proto["window_start"],
                window_end=proto["window_end"],
            )
            rows.append(
                {
                    "slug": r["slug"],
                    "label": r["label"],
                    "trader_id": spec["trader_id"],
                    "min_net_amount_m": r["min_net_amount_m"],
                    "oos_cost_sensitivity": costs,
                    "fillability": fill,
                }
            )
            print(
                f"{r['label']}: oos ret% 0bps={costs['cost_0bps']['period_return_pct']} "
                f"20bps={costs['cost_20bps']['period_return_pct']} "
                f"45bps={costs['cost_45bps']['period_return_pct']} | "
                f"locked%={fill['pct_entry_bar_locked']} "
                f"gap>=9%={fill['pct_entry_gap_ge_9pct']} "
                f"medTurnover={fill['median_signal_turnover_100m_ntd']}億"
            )
    finally:
        conn.close()

    out = REPORTS_RESEARCH / "foreign-branch-copytrade"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{date.today():%Y%m%d}_foreign_branch_copytrade_robustness.json"
    path.write_text(
        json.dumps({"protocol": proto, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("wrote", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
