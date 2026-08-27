#!/usr/bin/env python3
"""技術面因子面板 —— 籌碼因子測完全滅後，換因子族再測一次。

籌碼是**盤後公布的落後指標**，程式若靠它選股等於用昨天的資訊。
真正在跑程式的人更可能用價格形態、技術指標、事件。本檔建立技術面因子，
供 branch_consistency_scan 用同一套「集中度 vs 隨機基準」方法測。

因子分五族（全部只用 date ≤ T 的資料，PIT 乾淨）：
  趨勢   ma20_dev / ma60_dev / ma200_dev / ma_align
  動能   ret1 / ret5 / ret20 / ret60
  量能   vol_ratio（量/20日均量）/ amount（成交金額）
  波動   atr_pct / vol_squeeze（近20日振幅 / 近60日振幅）
  位置   hi52_dev / lo52_dev / rng_pos（當日收盤在當日高低的位置）
  形態   body_ratio（實體/全距）/ upper_wick / lower_wick / gap
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"

TECH = [
    ("ma20_dev", "距20MA"), ("ma60_dev", "距60MA"), ("ma200_dev", "距200MA"),
    ("ma_align", "均線排列"), ("ret1", "前日報酬"), ("ret5", "5日動能"),
    ("ret20", "20日動能"), ("ret60", "60日動能"), ("vol_ratio", "量比"),
    ("amount", "成交金額"), ("atr_pct", "波動幅度"), ("squeeze", "波動收縮"),
    ("hi52_dev", "距52週高"), ("lo52_dev", "距52週低"), ("rng_pos", "收盤位置"),
    ("body", "K棒實體"), ("uwick", "上影線"), ("lwick", "下影線"), ("gap", "跳空"),
    ("up_streak", "連漲天數"),
]


def build(start: str = "2023-01-01") -> pd.DataFrame:
    c = connect_ro()
    px = pd.read_sql_query(
        """SELECT stock_id, trade_date, source, open, high, low, close, volume/1000.0 AS vol
             FROM stock_daily_bars WHERE trade_date>=? AND close>0""", c, params=(start,))
    px["rk"] = px.source.map({"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}).fillna(9)
    d = (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
           .drop(columns=["rk", "source"]).sort_values(["stock_id", "trade_date"]))
    g = d.groupby("stock_id", group_keys=False)
    d["ret1"] = g.close.pct_change()
    for k in (5, 20, 60):
        d[f"ret{k}"] = g.close.pct_change(k)
    for k in (20, 60, 200):
        ma = g.close.transform(lambda s: s.rolling(k, min_periods=k // 2).mean())
        d[f"ma{k}_dev"] = d.close / ma - 1
    d["ma_align"] = (np.sign(d.ma20_dev) + np.sign(d.ma60_dev) + np.sign(d.ma200_dev)) / 3
    d["vol20"] = g.vol.transform(lambda s: s.rolling(20, min_periods=10).mean())
    d["vol_ratio"] = d.vol / d.vol20.replace(0, np.nan)
    d["amount"] = d.close * d.vol
    rng_ = (d.high - d.low) / d.close
    d["atr_pct"] = g.close.transform(lambda s: s.pct_change().rolling(20, min_periods=10).std())
    a20 = pd.Series(rng_.to_numpy(), index=d.index).groupby(d.stock_id).transform(
        lambda s: s.rolling(20, min_periods=10).mean())
    a60 = pd.Series(rng_.to_numpy(), index=d.index).groupby(d.stock_id).transform(
        lambda s: s.rolling(60, min_periods=30).mean())
    d["squeeze"] = a20 / a60.replace(0, np.nan)
    hi = g.high.transform(lambda s: s.rolling(243, min_periods=60).max())
    lo = g.low.transform(lambda s: s.rolling(243, min_periods=60).min())
    d["hi52_dev"] = d.close / hi - 1
    d["lo52_dev"] = d.close / lo - 1
    span = (d.high - d.low).replace(0, np.nan)
    d["rng_pos"] = (d.close - d.low) / span
    d["body"] = (d.close - d.open).abs() / span
    d["uwick"] = (d.high - d[["open", "close"]].max(axis=1)) / span
    d["lwick"] = (d[["open", "close"]].min(axis=1) - d.low) / span
    d["gap"] = d.open / g.close.shift(1) - 1
    up = (d.ret1 > 0).astype(int)
    d["up_streak"] = up.groupby((up != up.groupby(d.stock_id).shift()).cumsum()).cumsum() * up
    keep = ["stock_id", "trade_date"] + [c_ for c_, _ in TECH]
    return d[keep]


if __name__ == "__main__":
    d = build()
    d.to_pickle(DIR / "tech_panel.pkl")
    print(f"技術面板 {len(d):,} stock-day · {d.trade_date.nunique()} 日 · {d.stock_id.nunique()} 檔")
    print("覆蓋率：")
    for c_, n_ in TECH:
        print(f"  {n_:<10}{d[c_].notna().mean()*100:>6.1f}%")
    sys.exit(0)
