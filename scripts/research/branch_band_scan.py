#!/usr/bin/env python3
"""千萬級部位帶掃描 —— 用金額帶隔離出「不可能是散戶」的交易，再讓規律現身。

## 為什麼是千萬

先前失敗的兩端：
· 5 億門檻 → 抓到的是一次性大宗（單一客戶的調節），95 筆、無規律
· 不設門檻 → 散戶雜訊淹沒（每個分點都有 30~90 檔散戶當沖）

**千萬級（1,000 萬 ~ 1 億）是唯一能自然隔離的帶**：
散戶極少在單一檔做到千萬，但程式會反覆做。台股約 30% 成交來自程式交易，
它們必然在這個帶留下高頻、規律的痕跡。

## 方法
1. 逐日掃出所有 (分點, 股票) 中 gross 落在指定金額帶的交易
2. 對每個分點統計：帶內交易的頻率、規律性、當沖度、參與率
3. **規律性是關鍵**：程式會天天出現且數量穩定；人工是零星的
4. 對通過的分點做籌碼側寫（反向找出選股參數）

⚠️ 索引限制：分點表只能逐日查（見 skill）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

OUT = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"


def scan(start: str, lo: float, hi: float) -> pd.DataFrame:
    c = connect_ro()
    dates = [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM stock_daily_bars WHERE trade_date>=? ORDER BY trade_date",
        (start,))]
    px = pd.read_sql_query(
        """SELECT stock_id, trade_date, source, close, volume/1000.0 AS vol
             FROM stock_daily_bars WHERE trade_date>=? AND close>0""", c, params=(start,))
    px["rk"] = px.source.map({"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}).fillna(9)
    px = (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
            .drop(columns=["rk", "source"]).set_index(["stock_id", "trade_date"]))
    parts, t0 = [], time.time()
    for i, d in enumerate(dates):
        b = pd.read_sql_query(
            """SELECT securities_trader_id AS tid, stock_id, buy, sell
                 FROM stock_broker_branch_daily
                WHERE trade_date=? AND (buy>0 OR sell>0)""", c, params=(d,))
        if b.empty:
            continue
        p = px.xs(d, level="trade_date")
        b = b.join(p, on="stock_id", how="inner")
        b["gross"] = (b.buy + b.sell) * b.close
        b = b[(b.gross >= lo) & (b.gross < hi)]
        if b.empty:
            continue
        b["net"] = (b.buy - b.sell) * b.close
        b["rt"] = np.minimum(b.buy, b.sell) / np.maximum(b.buy, b.sell).replace(0, np.nan)
        b["part"] = (b.buy + b.sell) / 1000.0 / b.vol.replace(0, np.nan)
        b["trade_date"] = d
        parts.append(b[["tid", "stock_id", "trade_date", "gross", "net", "rt", "part", "close"]])
        if i % 100 == 99:
            print(f"  {i+1}/{len(dates)}　{(time.time()-t0)/60:.1f} 分", flush=True)
    return pd.concat(parts, ignore_index=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--lo", type=float, default=1e7, help="金額帶下界（預設 1000 萬）")
    ap.add_argument("--hi", type=float, default=1e8, help="金額帶上界（預設 1 億）")
    args = ap.parse_args()
    d = scan(args.start, args.lo, args.hi)
    d.to_pickle(OUT / "branch_band_trades.pkl")
    nd = d.trade_date.nunique()
    print(f"\n金額帶 {args.lo/1e4:,.0f}萬~{args.hi/1e4:,.0f}萬：{len(d):,} 筆　"
          f"{d.tid.nunique()} 個分點　{d.stock_id.nunique()} 檔　{nd} 日")
    g = d.groupby("tid")
    f = pd.DataFrame({
        "n": g.size(),
        "days": g.trade_date.nunique(),
        "stocks": g.stock_id.nunique(),
        "gross_med": g.gross.median(),
        "rt": g.rt.median(),
        "part": g.part.median(),
        "net_bias": g.net.sum() / g.gross.sum(),      # 方向偏好
    })
    per = d.groupby(["tid", "trade_date"]).size().rename("k").reset_index()
    pg = per.groupby("tid").k
    f["per_day"] = pg.median()
    f["per_day_cv"] = pg.std() / pg.mean().replace(0, np.nan)
    f["presence"] = f.days / nd
    # ★ 規律性：出席率高 × 每日筆數穩定 —— 程式的核心特徵
    f["regular"] = f.presence * (1 - f.per_day_cv.clip(0, 2) / 2)
    f = f[f.days >= 60].sort_values("regular", ascending=False)
    f.to_pickle(OUT / "branch_band_fingerprint.pkl")
    c = connect_ro()
    nm = {}
    for dd in ("2026-08-25", "2025-06-10"):
        for t, n in c.execute("SELECT DISTINCT securities_trader_id, securities_trader "
                              "FROM stock_broker_branch_daily WHERE trade_date=?", (dd,)):
            nm.setdefault(t, n)
    print(f"\n活躍 ≥60 日的分點 {len(f)} 個 —— 依規律性排序\n")
    print(f"{'分點':<7}{'名稱':<14}{'規律':>6}{'出席':>6}{'日筆數':>7}{'CV':>6}"
          f"{'總筆數':>7}{'檔數':>6}{'當沖度':>7}{'參與率':>8}{'中位額':>9}{'方向':>7}")
    for r in f.head(30).itertuples():
        print(f"{r.Index:<7}{str(nm.get(r.Index,'?'))[:13]:<14}{r.regular:>6.3f}{r.presence:>6.2f}"
              f"{r.per_day:>7.1f}{r.per_day_cv:>6.2f}{r.n:>7,}{r.stocks:>6}{r.rt:>7.3f}"
              f"{r.part*100:>7.2f}%{r.gross_med/1e4:>8.0f}萬{r.net_bias:>+7.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
