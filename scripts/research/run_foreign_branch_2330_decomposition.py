#!/usr/bin/env python3
"""外資分點 copytrade 的 alpha 是不是「台積電擇時」偽裝的？

各外資分點的密度配對訊號有 43–58% 就是 2330，所以把每筆 leg 的超額報酬
（T+1 開盤買 → 持有 H 交易日收盤賣，減 IX0001 同期）拆成三桶：

  * `2330`     — 台積電
  * `mega4`    — 2317 鴻海 / 2454 聯發科 / 2308 台達電
  * `rest`     — 其餘

若 alpha 幾乎只來自 2330 桶，那這不是「分點選股跟單」，而是一個台積電進場擇時訊號。

  PYTHONPATH=src .venv/bin/python scripts/research/run_foreign_branch_2330_decomposition.py
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics as st
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from analytics.bench import bench_return_entry_to_exit  # noqa: E402
from copytrade.branch_signals import iter_branch_amount_buy_signals  # noqa: E402
from report_paths import REPORTS_RESEARCH  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

MEGA4 = {"2317", "2454", "2308"}


def _forward_bars(
    conn: sqlite3.Connection, stock_id: str, after: str, n: int
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT trade_date, open, close
        FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' AND trade_date > ?
          AND close IS NOT NULL AND close > 0
        ORDER BY trade_date LIMIT ?
        """,
        (stock_id, after, n),
    ).fetchall()


def _bucket(stock_id: str) -> str:
    if stock_id == "2330":
        return "2330"
    return "mega4" if stock_id in MEGA4 else "rest"


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    n = len(xs)
    mean = st.mean(xs)
    sd = st.pstdev(xs) if n >= 2 else 0.0
    return {
        "n": n,
        "mean_excess_pct": round(mean, 4),
        "median_excess_pct": round(st.median(xs), 4),
        "t_stat": round(mean / (sd / math.sqrt(n)), 3) if sd > 1e-12 else None,
        "hit_rate_pct": round(100.0 * sum(1 for x in xs if x > 0) / n, 2),
        "sum_excess_pct": round(sum(xs), 2),
    }


def decompose(
    conn: sqlite3.Connection,
    trader_id: str,
    *,
    min_net_amount_ntd: float,
    hold: int,
    window_start: str,
    window_end: str,
) -> dict:
    signals = iter_branch_amount_buy_signals(
        conn,
        trader_id,
        min_net_amount_ntd=min_net_amount_ntd,
        top_n=1,
        window_start=window_start,
        window_end=window_end,
    )
    buckets: dict[str, list[float]] = {"2330": [], "mega4": [], "rest": []}
    all_x: list[float] = []
    for s in signals:
        bars = _forward_bars(conn, s.stock_id, s.signal_date, hold)
        if len(bars) < hold:
            continue
        entry_bar, exit_bar = bars[0], bars[-1]
        entry_px = entry_bar["open"]
        if entry_px is None or float(entry_px) <= 0:
            entry_px = entry_bar["close"]
        ret = 100.0 * (float(exit_bar["close"]) / float(entry_px) - 1.0)
        bench = bench_return_entry_to_exit(
            conn,
            str(entry_bar["trade_date"]),
            str(exit_bar["trade_date"]),
            entry_price_mode="open",
        )
        if bench is None:
            continue
        excess = ret - bench
        buckets[_bucket(s.stock_id)].append(excess)
        all_x.append(excess)
    return {
        "all": _stats(all_x),
        "by_bucket": {k: _stats(v) for k, v in buckets.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Foreign branch 2330 decomposition")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--compare-json",
        type=Path,
        default=REPORTS_RESEARCH
        / "foreign-branch-copytrade"
        / f"{date.today():%Y%m%d}_foreign_branch_copytrade_compare.json",
    )
    ap.add_argument("--hold", type=int, default=12)
    args = ap.parse_args()

    payload = json.loads(args.compare_json.read_text(encoding="utf-8"))
    proto = payload["protocol"]
    conn = connect(args.db)
    rows = []
    try:
        for r in payload["rank_density_matched_oos"]:
            spec = next(b for b in payload["branches"] if b["slug"] == r["slug"])
            amt = float(r["min_net_amount_m"]) * 1e6
            res = {}
            for wname, ws, we in (
                ("full", proto["window_start"], proto["window_end"]),
                ("oos", proto["oos_cut"], proto["window_end"]),
            ):
                res[wname] = decompose(
                    conn,
                    spec["trader_id"],
                    min_net_amount_ntd=amt,
                    hold=args.hold,
                    window_start=ws,
                    window_end=we,
                )
            rows.append(
                {
                    "slug": r["slug"],
                    "label": r["label"],
                    "trader_id": spec["trader_id"],
                    "min_net_amount_m": r["min_net_amount_m"],
                    **res,
                }
            )
            f = res["full"]
            print(
                f"{r['label']}({spec['trader_id']}) full: "
                f"ALL n={f['all']['n']} mean={f['all'].get('mean_excess_pct')} "
                f"t={f['all'].get('t_stat')} | "
                f"2330 n={f['by_bucket']['2330']['n']} "
                f"mean={f['by_bucket']['2330'].get('mean_excess_pct')} "
                f"t={f['by_bucket']['2330'].get('t_stat')} | "
                f"mega4 n={f['by_bucket']['mega4']['n']} "
                f"mean={f['by_bucket']['mega4'].get('mean_excess_pct')} | "
                f"rest n={f['by_bucket']['rest']['n']} "
                f"mean={f['by_bucket']['rest'].get('mean_excess_pct')} "
                f"t={f['by_bucket']['rest'].get('t_stat')}"
            )
    finally:
        conn.close()

    out = REPORTS_RESEARCH / "foreign-branch-copytrade"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{date.today():%Y%m%d}_foreign_branch_2330_decomposition.json"
    path.write_text(
        json.dumps(
            {"protocol": proto, "hold": args.hold, "rows": rows},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("wrote", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
