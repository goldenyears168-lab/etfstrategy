#!/usr/bin/env python3
"""開盤動能「相對台指期同步超額報酬」版進場訊號 vs 原始絕對版比較.

原始版（expert_pool_futures_open_breakout_scan.py）：進場門檻＝個股自己相對
自己開盤價漲跌 X%。

這版：進場門檻＝個股相對「台指期同步（同一時刻，用最近一筆不晚於個股tick時間
的TX成交價）」的超額漲跌 X%（個股報酬 − TX同步報酬）。只換進場邏輯，出場仍用
個股自身價格的移動停利（跟原始版一致），才能單獨看出「校正大盤」這個變因的
影響，不要同時換兩件事。

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/momentum_tx_relative_scan.py
"""

from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from math import erf
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parents[2] / "reports/research/expert_pool_futures_tick"
X_PCT = 0.5
TRAIL_PCT = 1.0


def load_times_prices(path: Path) -> dict[str, tuple[list[str], np.ndarray, str]]:
    day_rows: dict[str, list[tuple[str, float, str]]] = defaultdict(list)
    with path.open() as f:
        r = csv.DictReader(f)
        for row in r:
            if not row["price"]:
                continue
            d = row["date"][:10]
            day_rows[d].append((row["date"], float(row["price"]), row["contract_date"]))
    out = {}
    for d, rows in day_rows.items():
        rows.sort(key=lambda x: x[0])
        times = [x[0] for x in rows]
        prices = np.array([x[1] for x in rows], dtype=float)
        out[d] = (times, prices, rows[0][2] if rows else "")
    return out


def load_tx() -> dict[str, tuple[list[str], np.ndarray]]:
    tx = {}
    for path in sorted(DATA_DIR.glob("tx_market_TX_tick_*.csv")):
        d = path.name.replace("tx_market_TX_tick_", "").replace(".csv", "")
        rows = []
        with path.open() as f:
            r = csv.DictReader(f)
            for row in r:
                if not row["price"]:
                    continue
                rows.append((row["date"], float(row["price"])))
        rows.sort(key=lambda x: x[0])
        if rows:
            tx[d] = ([x[0] for x in rows], np.array([x[1] for x in rows], dtype=float))
    return tx


def align_to_tx(stock_times: list[str], tx_times: list[str], tx_prices: np.ndarray) -> np.ndarray:
    """對每個個股tick時間，取最近一筆『不晚於』該時間的TX價格."""
    idx = np.searchsorted(tx_times, stock_times, side="right") - 1
    idx = np.clip(idx, 0, len(tx_prices) - 1)
    return tx_prices[idx]


def simulate_relative(
    stock_prices: np.ndarray, tx_at_stock_ticks: np.ndarray, x_pct: float, trail_pct: float
) -> dict | None:
    if stock_prices.size < 2:
        return None
    stock_open = float(stock_prices[0])
    tx_open = float(tx_at_stock_ticks[0])
    stock_ret = (stock_prices - stock_open) / stock_open * 100.0
    tx_ret = (tx_at_stock_ticks - tx_open) / tx_open * 100.0
    excess = stock_ret - tx_ret

    long_hits = np.where(excess >= x_pct)[0]
    short_hits = np.where(excess <= -x_pct)[0]
    long_idx = int(long_hits[0]) if long_hits.size else None
    short_idx = int(short_hits[0]) if short_hits.size else None
    if long_idx is None and short_idx is None:
        return None
    if short_idx is None or (long_idx is not None and long_idx < short_idx):
        direction, entry_idx = "long", long_idx
    else:
        direction, entry_idx = "short", short_idx

    fill = float(stock_prices[entry_idx])
    remainder = stock_prices[entry_idx + 1 :]
    if remainder.size == 0:
        return {"direction": direction, "ret_pct": 0.0}

    seq = np.concatenate(([fill], remainder))
    if direction == "long":
        post_peak = np.maximum.accumulate(seq)[1:]
        stops = post_peak * (1 - trail_pct / 100.0)
        breach = np.where(remainder <= stops)[0]
        exit_price = float(stops[int(breach[0])]) if breach.size else float(remainder[-1])
        ret_pct = (exit_price - fill) / fill * 100.0
    else:
        post_trough = np.minimum.accumulate(seq)[1:]
        stops = post_trough * (1 + trail_pct / 100.0)
        breach = np.where(remainder >= stops)[0]
        exit_price = float(stops[int(breach[0])]) if breach.size else float(remainder[-1])
        ret_pct = (fill - exit_price) / fill * 100.0

    return {"direction": direction, "ret_pct": ret_pct}


def naive_ttest(returns):
    n = len(returns)
    if n < 2:
        return float("nan"), float("nan")
    mean = statistics.mean(returns)
    sd = statistics.stdev(returns)
    if sd == 0:
        return float("nan"), float("nan")
    t = mean / (sd / (n ** 0.5))
    p = 2 * (1 - 0.5 * (1 + erf(abs(t) / (2 ** 0.5))))
    return t, p


def main() -> int:
    tx = load_tx()
    print(f"TX 天數：{len(tx)}")

    per_stock = {}
    for path in sorted(DATA_DIR.glob("*.csv")):
        name = path.name
        if name.startswith("tx_market_"):
            continue
        sid = name.split("_")[1] if name.startswith("ctrl_") else name.split("_")[0]
        days = load_times_prices(path)
        per_stock.setdefault(sid, {}).update(days)
    print(f"個股標的數：{len(per_stock)}")

    per_stock_rows = []
    day_returns: dict[str, list[float]] = defaultdict(list)
    n_matched_days = 0
    n_skipped_no_tx = 0
    same_dir_count = 0
    diff_dir_count = 0

    for sid, days in per_stock.items():
        trades = []
        for d, (times, prices, contract) in days.items():
            if d not in tx:
                n_skipped_no_tx += 1
                continue
            tx_times, tx_prices = tx[d]
            tx_aligned = align_to_tx(times, tx_times, tx_prices)
            res = simulate_relative(prices, tx_aligned, X_PCT, TRAIL_PCT)
            if res:
                trades.append(res)
                day_returns[d].append(res["ret_pct"])
                n_matched_days += 1
        if trades:
            rets = [t["ret_pct"] for t in trades]
            per_stock_rows.append(
                {
                    "sid": sid,
                    "n": len(rets),
                    "win": sum(1 for r in rets if r > 0) / len(rets) * 100,
                    "mean": statistics.mean(rets),
                    "sum": sum(rets),
                }
            )

    per_stock_rows.sort(key=lambda r: r["mean"], reverse=True)
    print(f"\n=== 相對TX超額版 逐檔排名（x={X_PCT}% trail={TRAIL_PCT}%）前15 ===")
    for r in per_stock_rows[:15]:
        print(f"  {r['sid']:>6} n={r['n']:>3} win={r['win']:>5.1f}% mean={r['mean']:>7.3f}% sum={r['sum']:>7.1f}%")
    print(f"\n=== 相對TX超額版 逐檔排名 後10 ===")
    for r in per_stock_rows[-10:]:
        print(f"  {r['sid']:>6} n={r['n']:>3} win={r['win']:>5.1f}% mean={r['mean']:>7.3f}% sum={r['sum']:>7.1f}%")

    all_rets = [r for rets in day_returns.values() for r in rets]
    n_all, win_all = len(all_rets), sum(1 for r in all_rets if r > 0) / max(len(all_rets), 1) * 100
    mean_all = statistics.mean(all_rets) if all_rets else float("nan")
    print(f"\n=== 相對TX超額版 全體 ===")
    print(f"n={n_all} win={win_all:.1f}% mean={mean_all:.3f}%")

    day_means = {d: statistics.mean(v) for d, v in day_returns.items()}
    vals = list(day_means.values())
    n_days = len(vals)
    mean_d = statistics.mean(vals)
    sd_d = statistics.stdev(vals)
    t_d = mean_d / (sd_d / (n_days ** 0.5))
    p_d = 2 * (1 - 0.5 * (1 + erf(abs(t_d) / (2 ** 0.5))))
    n_pos_days = sum(1 for v in vals if v > 0)
    print(f"日層級檢定：n_days={n_days} day_mean={mean_d:.3f}% t={t_d:.2f} p={p_d:.4f} pos_days={n_pos_days}/{n_days}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
