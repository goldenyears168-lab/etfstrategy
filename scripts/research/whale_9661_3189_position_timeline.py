#!/usr/bin/env python3
"""9661 富邦新店 在 3189 景碩 的累積淨部位建倉時間軸分析.

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/whale_9661_3189_position_timeline.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH, connect

TRADER_ID = "9661"
STOCK_ID = "3189"
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"


def load(conn) -> pd.DataFrame:
    q = """
        SELECT b.trade_date, b.buy, b.sell, b.net, p.close, p.high, p.low
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = 'finmind'
        WHERE b.securities_trader_id = ? AND b.stock_id = ? AND b.source = 'finmind'
        ORDER BY b.trade_date
    """
    df = pd.read_sql_query(q, conn, params=(TRADER_ID, STOCK_ID))
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["net_amt"] = df["net"] * df["close"]
    return df


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    df = load(conn)
    conn.close()

    df["cum_net_shares"] = df["net"].cumsum()
    df["cum_net_amt_at_close"] = df["cum_net_shares"] * df["close"]

    print(f"[INFO] 9661/3189：{len(df)} 個有交易紀錄的交易日，"
          f"{df['trade_date'].min().date()} ~ {df['trade_date'].max().date()}")
    print(f"最終累積淨股數：{df['cum_net_shares'].iloc[-1]:,.0f} 股，"
          f"以最後收盤價估值：{df['cum_net_amt_at_close'].iloc[-1]/1e8:.2f} 億元")

    # 用 20 個交易日滾動視窗計算「淨買超速度」，找加碼波段
    df["roll20_net"] = df["net"].rolling(20, min_periods=5).sum()

    out_path = OUT_DIR / "whale_9661_3189_position_timeline.csv"
    df.to_csv(out_path, index=False)
    print(f"[OK] 完整時間軸寫入 {out_path}")

    # 找累積曲線的高低轉折點，切分區段：用簡單分段法——
    # 以「20交易日淨買超額」排序找出最強的連續加碼波段
    # 改用更直觀的方法：把整個時間軸切成若干個由 cum_net_shares 局部最小值到下一個局部最大值的區段

    cum = df["cum_net_shares"].values
    dates = df["trade_date"].values
    n = len(cum)

    # 用 scipy 找局部極值（先做輕度平滑，避免雜訊切太碎）
    from scipy.signal import argrelextrema
    smooth = pd.Series(cum).rolling(10, min_periods=1, center=True).mean().values
    order = 15
    minima = argrelextrema(smooth, np.less_equal, order=order)[0]
    maxima = argrelextrema(smooth, np.greater_equal, order=order)[0]

    # 去除相鄰重複
    def dedupe(idx_arr):
        out = []
        for i in idx_arr:
            if not out or i - out[-1] > order:
                out.append(i)
        return out

    minima = dedupe(list(minima))
    maxima = dedupe(list(maxima))

    print(f"\n[INFO] 偵測到局部低點 {len(minima)} 個，局部高點 {len(maxima)} 個（平滑後，order={order}）")

    # 建構加碼波段：從每個低點到下一個高點，累積淨增量最大的前幾個
    turning = sorted(set(minima) | set(maxima) | {0, n - 1})
    segments = []
    for a, b in zip(turning[:-1], turning[1:]):
        if b <= a:
            continue
        delta_shares = cum[b] - cum[a]
        d0, d1 = pd.Timestamp(dates[a]), pd.Timestamp(dates[b])
        px0, px1 = df["close"].iloc[a], df["close"].iloc[b]
        px_min = df["close"].iloc[a:b + 1].min()
        px_max = df["close"].iloc[a:b + 1].max()
        n_days = b - a
        segments.append({
            "start_date": d0.date(),
            "end_date": d1.date(),
            "n_trade_days_span": n_days,
            "delta_cum_shares": delta_shares,
            "delta_value_est_ntd": delta_shares * ((px0 + px1) / 2),
            "price_start": px0,
            "price_end": px1,
            "price_range_low": px_min,
            "price_range_high": px_max,
        })

    seg_df = pd.DataFrame(segments)
    seg_df = seg_df.sort_values("delta_cum_shares", ascending=False)
    seg_out = OUT_DIR / "whale_9661_3189_accumulation_segments.csv"
    seg_df.to_csv(seg_out, index=False)
    print(f"[OK] 分段（加碼/減碼波段）寫入 {seg_out}")

    print("\n=== 依累積增量排序的前10大波段（正=加碼，負=減碼） ===")
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(seg_df.head(10).to_string(index=False))

    print("\n=== 依時間順序列出所有波段 ===")
    seg_chrono = seg_df.sort_values("start_date")
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(seg_chrono.to_string(index=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
