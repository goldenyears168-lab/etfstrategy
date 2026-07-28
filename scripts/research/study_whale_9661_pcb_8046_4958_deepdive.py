#!/usr/bin/env python3
"""9661 富邦新店 在 8046 南電 / 4958 臻鼎-KY 的建倉時間軸 + 減碼分析 + 兩檔連動比較.

用法：
  PYTHONPATH=src .venv/bin/python study_whale_9661_pcb_deepdive.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path("/Users/jackm4/Documents/ETF/股票研究")
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH, connect

TRADER_ID = "9661"
STOCKS = ["8046", "4958"]
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"


def load_trader_stock(conn, stock_id: str) -> pd.DataFrame:
    q = """
        SELECT b.trade_date, b.buy, b.sell, b.net, p.close, p.open
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = 'finmind'
        WHERE b.securities_trader_id = ? AND b.stock_id = ? AND b.source = 'finmind'
        ORDER BY b.trade_date
    """
    df = pd.read_sql_query(q, conn, params=(TRADER_ID, stock_id))
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["net_amt"] = df["net"] * df["close"]
    df["cum_net_shares"] = df["net"].cumsum()
    df["cum_net_value"] = df["cum_net_shares"] * df["close"]
    df["stock_id"] = stock_id
    return df


def monthly_agg(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["ym"] = d["trade_date"].dt.strftime("%Y-%m")
    g = d.groupby("ym").agg(
        net_shares=("net", "sum"),
        net_amt=("net_amt", "sum"),
        close_end=("close", "last"),
        cum_net_shares_end=("cum_net_shares", "last"),
        cum_net_value_end=("cum_net_value", "last"),
    )
    return g


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    data = {sid: load_trader_stock(conn, sid) for sid in STOCKS}
    conn.close()

    for sid, df in data.items():
        m = monthly_agg(df)
        out_path = OUT_DIR / f"whale_9661_{sid}_monthly_position.csv"
        m.to_csv(out_path)
        print(f"[OK] {sid} 月度部位寫入 {out_path}  (共 {len(m)} 個月)")

    # ---- 4958 減碼分析：找出 cum_net_shares 從峰值大幅回落的區間 ----
    print("\n=== 4958 臻鼎 累積淨部位峰值與回落分析 ===")
    d = data["4958"][["trade_date", "cum_net_shares", "close"]].copy()
    d["date_str"] = d["trade_date"].dt.strftime("%Y-%m-%d")
    cum = d["cum_net_shares"].values
    dates = d["date_str"].values
    closes = d["close"].values
    n = len(cum)

    # running max and its position
    run_max = np.maximum.accumulate(cum)
    # find biggest drawdown from any prior peak, in shares
    drawdown = run_max - cum
    max_dd_idx = int(np.argmax(drawdown))
    # peak index: last index where cum == run_max[max_dd_idx] before max_dd_idx
    peak_val = run_max[max_dd_idx]
    peak_idx = int(np.where(cum[: max_dd_idx + 1] == peak_val)[0].max())

    print(f"最大回落（股數）：峰值 {cum[peak_idx]:,.0f} 股 @ {dates[peak_idx]} (收盤 {closes[peak_idx]:.1f})")
    print(f"                 -> 谷值 {cum[max_dd_idx]:,.0f} 股 @ {dates[max_dd_idx]} (收盤 {closes[max_dd_idx]:.1f})")
    print(f"                 回落幅度 {drawdown[max_dd_idx]:,.0f} 股 ({drawdown[max_dd_idx]/peak_val*100:.1f}% of peak)")
    print(f"減碼期間股價變化：{closes[peak_idx]:.1f} -> {closes[max_dd_idx]:.1f} ({(closes[max_dd_idx]/closes[peak_idx]-1)*100:+.1f}%)")

    # final vs peak
    final_cum = cum[-1]
    final_close = closes[-1]
    print(f"\n目前（最後交易日 {dates[-1]}）累積淨部位：{final_cum:,.0f} 股，收盤 {final_close:.1f}")
    print(f"目前部位 / 歷史峰值 = {final_cum/peak_val*100:.1f}% (retention_ratio)")

    # top 10 largest single-month net sell for 4958, and what 8046 net was in same month
    print("\n=== 4958 月度淨賣超最大的月份（找減碼時間點）+ 同期 8046 動作 ===")
    m4958 = monthly_agg(data["4958"])
    m8046 = monthly_agg(data["8046"])
    combo = m4958[["net_shares", "net_amt", "close_end", "cum_net_shares_end"]].join(
        m8046[["net_shares", "net_amt", "close_end", "cum_net_shares_end"]],
        lsuffix="_4958", rsuffix="_8046", how="outer",
    )
    combo = combo.sort_index()
    worst10 = combo.sort_values("net_amt_4958").head(15)
    print(worst10.to_string())

    combo_out = OUT_DIR / "whale_9661_8046_4958_monthly_combo.csv"
    combo.to_csv(combo_out)
    print(f"\n[OK] 兩檔月度合併表寫入 {combo_out}")

    # correlation of monthly net_amt between the two stocks (co-movement test)
    both = combo.dropna(subset=["net_amt_4958", "net_amt_8046"])
    corr = both["net_amt_4958"].corr(both["net_amt_8046"])
    corr_shares = both["net_shares_4958"].corr(both["net_shares_8046"])
    print(f"\n兩檔月度淨買賣金額相關係數 (n={len(both)} 個共同月份): {corr:.3f}")
    print(f"兩檔月度淨買賣股數相關係數: {corr_shares:.3f}")

    # sign agreement rate: fraction of months where both are net buy or both net sell
    sign_agree = ((both["net_amt_4958"] > 0) == (both["net_amt_8046"] > 0)).mean()
    print(f"月度同向（同買或同賣）比例: {sign_agree*100:.1f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
