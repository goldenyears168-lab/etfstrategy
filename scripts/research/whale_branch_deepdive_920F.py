#!/usr/bin/env python3
"""920F 深挖：分點名稱、建倉型態（Top10最頻繁個股）、擇時品質（20日高低百分位 vs 市場基準）。

這支腳本需要在 mini 上跑（stock_broker_branch_daily / stock_daily_bars 全分點覆蓋資料只在 mini）。
產出寫到 reports/research/branch-footprint-screen/whale_branch_l1h7_deepdive_920F_*.csv

Run (on mini):
  PYTHONPATH=src .venv/bin/python3 scripts/research/whale_branch_deepdive_920F.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from stock_db import connect, DEFAULT_DB_PATH

TRADER_ID = "920F"
START = "2024-07-01"
OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
OUT_DIR.mkdir(parents=True, exist_ok=True)

conn = connect(DEFAULT_DB_PATH)


def get_trader_name() -> str:
    row = conn.execute(
        "SELECT DISTINCT securities_trader FROM stock_broker_branch_daily "
        "WHERE securities_trader_id=? AND source='finmind' LIMIT 1",
        (TRADER_ID,),
    ).fetchone()
    return row[0] if row else "UNKNOWN"


def load_events_local() -> pd.DataFrame:
    """讀取本地已算好的 L1H7 events.csv，篩出 920F 的 141 筆事件。"""
    df = pd.read_csv(
        OUT_DIR / "whale_branch_l1h7_universe_events.csv",
        dtype={"stock_id": str, "securities_trader_id": str},
    )
    return df[df["securities_trader_id"] == TRADER_ID].copy()


def cumulative_position(events: pd.DataFrame, top_n: int = 10):
    """Top N 最頻繁個股的累積淨買超部位時間序列（用全部 920F 交易，不限 Top1買方事件）。"""
    top_stocks = (
        events.groupby("stock_id")["buy_amt"].agg(["count", "sum"]).sort_values("count", ascending=False).head(top_n)
    )
    stock_ids = top_stocks.index.tolist()
    placeholders = ",".join(["?"] * len(stock_ids))
    q = f"""
    SELECT trade_date, stock_id, net
    FROM stock_broker_branch_daily
    WHERE source='finmind' AND securities_trader_id=? AND trade_date>=? AND stock_id IN ({placeholders})
    ORDER BY stock_id, trade_date
    """
    all_trades = pd.read_sql_query(q, conn, params=(TRADER_ID, START, *stock_ids))

    summaries = []
    for sid in stock_ids:
        g = all_trades[all_trades["stock_id"] == sid].copy().sort_values("trade_date")
        g["cum_net"] = g["net"].cumsum()
        max_pos = g["cum_net"].max()
        min_pos = g["cum_net"].min()
        final_pos = g["cum_net"].iloc[-1] if len(g) else np.nan
        n_top1_events = int(top_stocks.loc[sid, "count"])
        n_buy_days = int((g["net"] > 0).sum())
        n_sell_days = int((g["net"] < 0).sum())
        final_frac_of_max = final_pos / max_pos if max_pos and max_pos > 0 else np.nan
        summaries.append({
            "stock_id": sid,
            "n_top1_l1h7_events": n_top1_events,
            "n_total_trade_days": len(g),
            "n_buy_days": n_buy_days,
            "n_sell_days": n_sell_days,
            "max_cum_net_shares": max_pos,
            "min_cum_net_shares": min_pos,
            "final_cum_net_shares": final_pos,
            "final_frac_of_max": final_frac_of_max,
        })
    return pd.DataFrame(summaries)


def timing_percentile(events: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """訊號日(signal_date)當天收盤在 trailing 20日高低區間的百分位，跟全市場基準比較。"""
    stock_ids = events["stock_id"].unique().tolist()
    placeholders = ",".join(["?"] * len(stock_ids))
    bars = pd.read_sql_query(
        f"""
        SELECT stock_id, trade_date, close
        FROM stock_daily_bars
        WHERE source='finmind' AND stock_id IN ({placeholders})
        ORDER BY stock_id, trade_date
        """,
        conn,
        params=stock_ids,
    )
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bars = bars.sort_values(["stock_id", "trade_date"])
    bars["roll_min20"] = bars.groupby("stock_id")["close"].transform(lambda s: s.rolling(20, min_periods=10).min())
    bars["roll_max20"] = bars.groupby("stock_id")["close"].transform(lambda s: s.rolling(20, min_periods=10).max())
    bars["pctile"] = (bars["close"] - bars["roll_min20"]) / (bars["roll_max20"] - bars["roll_min20"])
    bars_idx = bars.set_index(["stock_id", "trade_date"])["pctile"]

    events = events.copy()
    events["signal_date_dt"] = pd.to_datetime(events["signal_date"])
    vals = []
    for _, row in events.iterrows():
        key = (row["stock_id"], row["signal_date_dt"])
        vals.append(bars_idx.get(key, np.nan))
    events["timing_pctile"] = vals

    # 全市場基準：所有股票所有交易日的百分位分布（跟 9227/9217 script 一致的做法，但這次只抓 920F 涉及的103檔
    # 股票的完整歷史百分位分布，作為「隨機挑一天買」的參照）
    market_baseline = bars["pctile"].dropna()
    return events, market_baseline


def main():
    name = get_trader_name()
    print(f"920F 分點名稱: {name}")

    events = load_events_local()
    print(f"920F L1H7 events: n={len(events)}, distinct stocks={events['stock_id'].nunique()}")

    # (b) position building — top10 most frequently traded stocks
    print("\n--- (b) 建倉型態 (Top10 交易最頻繁個股，累積淨部位) ---")
    pos_summary = cumulative_position(events, top_n=10)
    print(pos_summary.to_string(index=False))
    pos_summary.to_csv(OUT_DIR / "whale_branch_l1h7_deepdive_920F_position_building.csv", index=False)

    # (c) timing quality
    print("\n--- (c) 擇時品質 (20日高低百分位 vs 市場基準, n=141 較大樣本統計檢定力較強) ---")
    events_t, market_baseline = timing_percentile(events)
    events_t.to_csv(OUT_DIR / "whale_branch_l1h7_deepdive_920F_events_with_timing.csv", index=False)

    tp = events_t["timing_pctile"].dropna()
    print(f"920F 買進日擇時百分位: n={len(tp)}, mean={tp.mean():.3f}, median={tp.median():.3f}, std={tp.std():.3f}")
    print(f"市場基準(103檔股票全歷史全交易日): n={len(market_baseline)}, mean={market_baseline.mean():.3f}, median={market_baseline.median():.3f}")
    ks = stats.ks_2samp(tp, market_baseline)
    print(f"KS test (920F vs 市場基準): stat={ks.statistic:.4f}, p={ks.pvalue:.4g}")
    t_pct, p_pct = stats.ttest_1samp(tp, market_baseline.mean())
    print(f"one-sample t-test (920F pctile mean vs 市場基準mean {market_baseline.mean():.3f}): t={t_pct:.3f}, p={p_pct:.4g}")

    print("\nDone.")


if __name__ == "__main__":
    main()
