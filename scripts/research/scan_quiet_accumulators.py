#!/usr/bin/env python3
"""H-QUIET-ACCUMULATOR · 找「安靜建倉者」，不是「單日爆量者」。

跟 scan_whale_buy_concentration.py / study_whale_branch_l1h7_universe.py 的偵測邏輯完全不同：
那兩支找的是「單日 Top1 買方」，天生偏向抓到造市/大宗轉手這種一次性大單；這支改成找
「(分點, 股票) 配對裡，長期、持續、低波動地累積淨部位」的型態——這才比較像真正耐心的
長期投資人，而不是單日爆量的中介帳戶。

方法：
1. 對每個 (securities_trader_id, stock_id)，只考慮有出手（buy>0 or sell>0）的交易日，
   要求至少 MIN_DAYS 個交易日（預設60）才夠格談「長期」。
2. 算累積淨部位（cumsum of net股數）逐日序列。
3. 三個篩選指標：
   - consistency：出手日裡淨買超（net>0）天數的比例——高代表持續同方向，不是來回震盪
   - trend_r2：累積部位對時間索引做線性迴歸的 R²——高代表平滑單向累積，不是鋸齒狀
   - retention：期末部位 / 期間最大部位——接近1代表建倉後真的抱著，不是建了又倒光
4. 對通過篩選的配對，算「已實現報酬」代理指標：用加權平均成本（用每日淨買金額加權的
   close價）vs 累積部位到達最大值那天之後的最新收盤價，算報酬率。

用法（在有完整分點資料的機器，例如 mini，跑）：
  PYTHONPATH=src .venv/bin/python scripts/research/scan_quiet_accumulators.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH, connect

SOURCE = "finmind"
MIN_DAYS = 60
MEGA_BLACKLIST_PATH = (
    ROOT / "reports/research/branch-footprint-screen/ab58_xMega_copytrade/mega_blacklist_v1.json"
)
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"


def load_mega_blacklist() -> set[str]:
    data = json.loads(MEGA_BLACKLIST_PATH.read_text(encoding="utf-8"))
    return set(data["symbols"])


def find_qualifying_pairs(conn, min_days: int) -> pd.DataFrame:
    """哪些 (分點,股票) 配對有 >= min_days 個出手交易日。用 SQL GROUP BY，避免拉全部原始列。"""
    q = f"""
        SELECT securities_trader_id, stock_id, COUNT(*) as n_days,
               MIN(trade_date) as first_date, MAX(trade_date) as last_date
        FROM stock_broker_branch_daily
        WHERE source = ? AND trade_date >= '2024-07-01' AND (buy > 0 OR sell > 0)
        GROUP BY securities_trader_id, stock_id
        HAVING COUNT(*) >= ?
    """
    return pd.read_sql_query(q, conn, params=(SOURCE, min_days))


def load_daily_for_pairs(conn, pairs: pd.DataFrame) -> pd.DataFrame:
    """只拉合格配對的完整逐日 buy/sell/net + 當日收盤價（算加權成本用）。"""
    trader_ids = tuple(pairs["securities_trader_id"].unique().tolist())
    stock_ids = tuple(pairs["stock_id"].unique().tolist())
    tph = ",".join("?" * len(trader_ids))
    sph = ",".join("?" * len(stock_ids))
    q = f"""
        SELECT b.securities_trader_id, b.stock_id, b.trade_date, b.buy, b.sell, b.net, p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = '{SOURCE}'
        WHERE b.source = '{SOURCE}' AND b.trade_date >= '2024-07-01'
          AND b.securities_trader_id IN ({tph}) AND b.stock_id IN ({sph})
          AND (b.buy > 0 OR b.sell > 0)
        ORDER BY b.securities_trader_id, b.stock_id, b.trade_date
    """
    return pd.read_sql_query(q, conn, params=(*trader_ids, *stock_ids))


def trend_r2(y: np.ndarray) -> float:
    n = len(y)
    if n < 3 or np.all(y == y[0]):
        return 0.0
    x = np.arange(n)
    slope, intercept = np.polyfit(x, y, 1)
    y_hat = slope * x + intercept
    ss_res = np.sum((y - y_hat) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    if ss_tot == 0:
        return 0.0
    return 1.0 - ss_res / ss_tot


def analyze_pair(g: pd.DataFrame) -> dict:
    g = g.sort_values("trade_date").reset_index(drop=True)
    net = g["net"].to_numpy(dtype=float)
    cum = np.cumsum(net)
    buy_amt = (g["buy"] * g["close"]).to_numpy()
    sell_amt = (g["sell"] * g["close"]).to_numpy()

    n_days = len(g)
    n_pos_days = int((net > 0).sum())
    consistency = n_pos_days / n_days if n_days else 0.0
    r2 = trend_r2(cum)
    max_cum = cum.max()
    min_cum = cum.min()
    final_cum = cum[-1]
    retention = final_cum / max_cum if max_cum > 0 else np.nan

    total_buy_shares = g.loc[g["buy"] > 0, "buy"].sum()
    total_buy_amt = buy_amt[g["buy"] > 0].sum() if (g["buy"] > 0).any() else 0.0
    weighted_avg_cost = total_buy_amt / total_buy_shares if total_buy_shares > 0 else np.nan

    latest_close = g["close"].iloc[-1]
    peak_idx = int(np.argmax(cum))
    peak_date = g["trade_date"].iloc[peak_idx]
    peak_close = g["close"].iloc[peak_idx]
    realized_proxy_pct = (
        (peak_close / weighted_avg_cost - 1.0) * 100 if weighted_avg_cost and weighted_avg_cost > 0 else np.nan
    )
    latest_proxy_pct = (
        (latest_close / weighted_avg_cost - 1.0) * 100 if weighted_avg_cost and weighted_avg_cost > 0 else np.nan
    )

    return {
        "securities_trader_id": g["securities_trader_id"].iloc[0],
        "stock_id": g["stock_id"].iloc[0],
        "n_days": n_days,
        "first_date": g["trade_date"].iloc[0],
        "last_date": g["trade_date"].iloc[-1],
        "consistency": round(consistency, 4),
        "trend_r2": round(r2, 4),
        "retention": round(retention, 4) if not np.isnan(retention) else None,
        "max_cum_shares": round(max_cum, 0),
        "final_cum_shares": round(final_cum, 0),
        "weighted_avg_cost": round(weighted_avg_cost, 3) if not np.isnan(weighted_avg_cost) else None,
        "peak_date": peak_date,
        "peak_close": round(peak_close, 3),
        "latest_close": round(latest_close, 3),
        "realized_proxy_pct": round(realized_proxy_pct, 2) if not np.isnan(realized_proxy_pct) else None,
        "latest_proxy_pct": round(latest_proxy_pct, 2) if not np.isnan(latest_proxy_pct) else None,
        "total_buy_amt": round(total_buy_amt, 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-days", type=int, default=MIN_DAYS)
    ap.add_argument("--min-consistency", type=float, default=0.65)
    ap.add_argument("--min-trend-r2", type=float, default=0.7)
    ap.add_argument("--min-retention", type=float, default=0.7)
    args = ap.parse_args()

    mega = load_mega_blacklist()
    conn = connect(DEFAULT_DB_PATH)

    t0 = time.time()
    print(f"[INFO] finding (branch,stock) pairs with >= {args.min_days} active days...")
    pairs = find_qualifying_pairs(conn, args.min_days)
    pairs = pairs[~pairs["stock_id"].isin(mega)]
    pairs = pairs[~pairs["stock_id"].str.startswith("00")]
    print(f"[INFO] qualifying pairs: {len(pairs):,} · elapsed {time.time()-t0:.1f}s")

    if pairs.empty:
        print("[WARN] no qualifying pairs, exiting")
        return 0

    t1 = time.time()
    print(f"[INFO] loading full daily series for {pairs['securities_trader_id'].nunique()} branches x "
          f"{pairs['stock_id'].nunique()} stocks...")
    daily = load_daily_for_pairs(conn, pairs)
    conn.close()
    print(f"[INFO] daily rows loaded: {len(daily):,} · elapsed {time.time()-t1:.1f}s")

    qualifying_keys = set(zip(pairs["securities_trader_id"], pairs["stock_id"]))
    daily["key"] = list(zip(daily["securities_trader_id"], daily["stock_id"]))
    daily = daily[daily["key"].isin(qualifying_keys)].drop(columns="key")

    t2 = time.time()
    print("[INFO] computing per-pair position/trend/retention metrics...")
    records = []
    for (tid, sid), g in daily.groupby(["securities_trader_id", "stock_id"], sort=False):
        if len(g) < args.min_days:
            continue
        records.append(analyze_pair(g))
    result = pd.DataFrame(records)
    print(f"[INFO] analyzed {len(result):,} pairs · elapsed {time.time()-t2:.1f}s")

    result.to_csv(OUT_DIR / "quiet_accumulators_all_pairs.csv", index=False)

    candidates = result[
        (result["consistency"] >= args.min_consistency)
        & (result["trend_r2"] >= args.min_trend_r2)
        & (result["retention"] >= args.min_retention)
        & (result["realized_proxy_pct"].notna())
    ].sort_values("realized_proxy_pct", ascending=False)
    candidates.to_csv(OUT_DIR / "quiet_accumulators_candidates.csv", index=False)

    print(f"\n[INFO] 候選（consistency>={args.min_consistency}, trend_r2>={args.min_trend_r2}, "
          f"retention>={args.min_retention}）: {len(candidates):,}")
    print(candidates.head(30).to_string())

    # 分點層級彙總：哪些分點名下有多個候選配對（代表這個分點整體風格就是耐心建倉）
    by_branch = (
        candidates.groupby("securities_trader_id")
        .agg(n_candidate_stocks=("stock_id", "nunique"), median_realized_pct=("realized_proxy_pct", "median"))
        .sort_values("n_candidate_stocks", ascending=False)
    )
    by_branch.to_csv(OUT_DIR / "quiet_accumulators_by_branch.csv")
    print("\n[INFO] 分點層級（依候選檔數排序，前20名）：")
    print(by_branch.head(20).to_string())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
