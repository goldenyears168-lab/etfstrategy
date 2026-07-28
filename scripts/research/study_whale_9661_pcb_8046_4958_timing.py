#!/usr/bin/env python3
"""9661 富邦新店 在 8046 南電 / 4958 臻鼎-KY 的買進擇時分析（20日區間百分位）+ campaign 切分.

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9661_pcb_8046_4958_timing.py
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
STOCKS = ["8046", "4958"]
CAMPAIGN_GAP = 10
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"


def load_buy_days(conn, stock_id: str) -> pd.DataFrame:
    q = """
        SELECT b.trade_date, b.buy, p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = 'finmind'
        WHERE b.securities_trader_id = ? AND b.stock_id = ? AND b.source = 'finmind' AND b.buy > 0
        ORDER BY b.trade_date
    """
    df = pd.read_sql_query(q, conn, params=(TRADER_ID, stock_id))
    df["buy_amt"] = df["buy"] * df["close"]
    return df


def load_bars(conn, stock_id: str) -> pd.DataFrame:
    q = """
        SELECT trade_date, open, close FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' ORDER BY trade_date
    """
    return pd.read_sql_query(q, conn, params=(stock_id,)).set_index("trade_date")


def timing_percentile(buy_days: pd.DataFrame, bars: pd.DataFrame, window: int = 20) -> pd.Series:
    close = bars["close"]
    roll_min = close.rolling(window).min()
    roll_max = close.rolling(window).max()
    pct = (close - roll_min) / (roll_max - roll_min)
    pct = pct.reindex(buy_days["trade_date"])
    return pct.reset_index(drop=True)


def merge_campaigns(dates: list[str], all_dates: list[str], gap: int = CAMPAIGN_GAP) -> list[str]:
    idx = {d: i for i, d in enumerate(all_dates)}
    dates = sorted(dates)
    campaigns = []
    cur_start = dates[0]
    last_idx = idx.get(dates[0])
    for d in dates[1:]:
        di = idx.get(d)
        if last_idx is not None and di is not None and di - last_idx <= gap:
            last_idx = di
            continue
        campaigns.append(cur_start)
        cur_start = d
        last_idx = di
    campaigns.append(cur_start)
    return campaigns


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    for stock_id in STOCKS:
        buy_days = load_buy_days(conn, stock_id)
        bars = load_bars(conn, stock_id)
        pct = timing_percentile(buy_days, bars, window=20)
        buy_days = buy_days.copy()
        buy_days["pct20"] = pct.values

        print(f"\n=== {stock_id} 擇時分析（全部買進日, n={len(buy_days)}）===")
        print("20日區間百分位分布（0=區間低點, 1=區間高點）：")
        print(buy_days["pct20"].dropna().describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]))
        low_third = (buy_days["pct20"] <= 0.33).mean()
        mid_third = ((buy_days["pct20"] > 0.33) & (buy_days["pct20"] <= 0.67)).mean()
        high_third = (buy_days["pct20"] > 0.67).mean()
        print(f"低檔1/3佔比: {low_third*100:.1f}%  中檔1/3佔比: {mid_third*100:.1f}%  高檔1/3佔比: {high_third*100:.1f}%")

        # 大單 vs 小單擇時比較（用金額前20% vs 後80%）
        floor80 = buy_days["buy_amt"].quantile(0.80)
        big = buy_days[buy_days["buy_amt"] >= floor80]
        small = buy_days[buy_days["buy_amt"] < floor80]
        print(f"\n大單（前20%，≥{floor80/1e6:.2f}百萬, n={len(big)}）20日百分位中位數: {big['pct20'].median():.3f}")
        print(f"小單（後80%，n={len(small)}）20日百分位中位數: {small['pct20'].median():.3f}")

        # campaign 切分
        all_dates = bars.index.tolist()
        camp_starts = merge_campaigns(buy_days["trade_date"].tolist(), all_dates, gap=CAMPAIGN_GAP)
        print(f"\n以 {CAMPAIGN_GAP} 交易日 gap 合併後，共 {len(camp_starts)} 個獨立買進 campaign")
        print(f"前5個 campaign 起始日: {camp_starts[:5]}")
        print(f"後5個 campaign 起始日: {camp_starts[-5:]}")

        out = buy_days[["trade_date", "buy_amt", "close", "pct20"]]
        out_path = OUT_DIR / f"whale_9661_{stock_id}_timing_percentile.csv"
        out.to_csv(out_path, index=False)
        print(f"[OK] 寫入 {out_path}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
