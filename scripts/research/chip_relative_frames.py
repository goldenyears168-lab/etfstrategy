#!/usr/bin/env python3
"""籌碼因子的四種表示法：水位 / 變化量 / 自身歷史 / 同業相對。

使用者 2026-08-27 的方法論修正：先前比較「最好 vs 最差的大單」時，用的是
**當日截面百分位的快照**（水位）—— 那只回答「他們買的股票在市場上排第幾」，
不回答「這檔的籌碼**變了什麼**」。真正有意義的應該是相對值：

  LEVEL  當日截面百分位              ← 先前唯一用的，差距只有 0.4~1.4 個百分位
  DELTA  相對前一日的變化量，再截面排序
  TS     相對自身過去 60 日的 z 分數（自己跟自己比）
  IND    同產業內的截面百分位（概念股/同業相對）

四種一起測，看最好與最差的大單在哪一種表示法下才分得開。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib.machinery import SourceFileLoader

LAB = SourceFileLoader("lab", str(Path(__file__).resolve().parent / "chip_lab.py")).load_module()
IND_CACHE = LAB.DIR / "stock_industry.pkl"

FACTORS = {
    "big_pct": "大戶持股", "ret_pct": "散戶持股", "holders": "股東人數",
    "sbl_pct": "借券佔股本", "util": "券源使用率", "fee_rate_vw": "借券費率",
    "sbl_volr": "借券量/量", "f_for5": "外資5日", "f_itc5": "投信5日",
    "f_conc": "分點集中度", "turn": "週轉率",
}


def industry() -> pd.DataFrame:
    """FinMind TaiwanStockInfo → stock_id → industry_category（含快取）。"""
    if IND_CACHE.exists():
        return pd.read_pickle(IND_CACHE)
    import requests
    tok = os.environ.get("FINMIND_TOKEN", "")
    r = requests.get("https://api.finmindtrade.com/api/v4/data",
                     params={"dataset": "TaiwanStockInfo", "token": tok}, timeout=120)
    r.raise_for_status()
    df = pd.DataFrame(r.json()["data"])[["stock_id", "industry_category"]]
    df = df.drop_duplicates("stock_id").rename(columns={"industry_category": "ind"})
    df.to_pickle(IND_CACHE)
    return df


def build(d: pd.DataFrame) -> pd.DataFrame:
    """對每個因子產生四種表示法，全部轉成 0~100 的截面百分位以便直接比較。"""
    d = d.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)
    ind = industry()
    d = d.merge(ind, on="stock_id", how="left")
    d["ind"] = d["ind"].fillna("未分類")
    g = d.groupby("stock_id", sort=False)
    for c in FACTORS:
        if c not in d.columns:
            continue
        # LEVEL：當日截面百分位
        d[f"L_{c}"] = d.groupby("trade_date")[c].rank(pct=True) * 100
        # DELTA：相對前一日的變化，再截面排序（變化量本身尺度不可比）
        d[f"_d{c}"] = g[c].diff()
        d[f"D_{c}"] = d.groupby("trade_date")[f"_d{c}"].rank(pct=True) * 100
        # TS：相對自身過去 60 日（PIT：shift(1) 不含當日）
        m = g[c].transform(lambda s: s.shift(1).rolling(60, min_periods=20).mean())
        sd = g[c].transform(lambda s: s.shift(1).rolling(60, min_periods=20).std())
        d[f"_z{c}"] = (d[c] - m) / sd.replace(0, np.nan)
        d[f"T_{c}"] = d.groupby("trade_date")[f"_z{c}"].rank(pct=True) * 100
        # IND：同產業內的截面百分位
        d[f"I_{c}"] = d.groupby(["trade_date", "ind"])[c].rank(pct=True) * 100
    return d


if __name__ == "__main__":
    ind = industry()
    print(f"產業分類 {len(ind)} 檔　{ind['ind'].nunique()} 個產業")
    print(ind["ind"].value_counts().head(12).to_string())

# ═══════════════════════════════════════════════════════════════════════
# 2026-08-27 結果 —— 使用者的方法論修正救活了一個已判死的因子
#
# 起因：先前比較「最好 vs 最差的大單」只用了 LEVEL（當日截面百分位快照），
# 使用者指出應該看相對值 —— 相對前一日、相對同業、相對自身歷史。
#
# 【四框架在大單結果上的樣本外表現】（159 萬筆方向明確千萬級大單）
#   水位        樣本內 +0.1883%  樣本外 +0.0594%  t=+6.20  衰減 68%
#   變化量      樣本內 +0.0488%  樣本外 +0.0136%  t=+1.29  衰減 72%
#   自身歷史    樣本內 +0.0504%  樣本外 +0.0701%  t=+7.19  衰減 −39%（唯一沒衰減）
#   同業相對    樣本內 +0.1449%  樣本外 +0.0474%  t=+5.03  衰減 67%
#   四框架合併  樣本內 +0.1912%  樣本外 −0.0053%  t=−0.53  ← 合併直接過擬變負
#
# 【但換成真實選股（凍結協定 + 真日報酬淨值）結論反轉】
# 自身歷史贏了大單測試卻是選股測試最弱的（K=1 最大 |t| 僅 1.62）。
# 跳出來的是 **同業相對**，而且改善集中在借券系列：
#
#   大型股 24 檔 · K=5 · 對同層等權的超額（真日報酬、K 重疊分批、扣進場成本跳空）
#     借券佔股本  水位 +7.06%/年 t=+0.90 (2025 −7.8%)
#                 同業相對 **+18.45%/年 IR 1.67 t=+2.63 p=0.009**（2024+15.9 2025+3.5 2026+45.6，逐年全正）
#     借券費率    水位 +4.95%/年 t=+0.75  →  同業相對 +12.19%/年 IR 1.41 t=+2.21
#
# 機制：借券費率與借券水位本來就有強烈的產業結構差異（半導體/金融/傳產的券源
# 市場天差地遠），全市場排序主要在排**產業**不是排個股。同業相對剝掉那一層。
#
# 【擋掉「88 格挑一格」的框架層級檢定】
# 事先固定方向、11 個因子全測、都在可交易空間：
#   平均超額 水位 −0.85% → 同業相對 +3.56%（改善 +4.41pp）
#   8/11 個因子變好　Wilcoxon 配對 p=0.0137  → 改善是**框架層級**的，不是單格運氣
#
# 【仍存在的弱點】
#   · 成本敏感：+0.2% 衝擊成本 → t 掉到 1.54；極端 1.0% → t=0.80
#   · 2026 仍主導（+45.6% vs 2025 +3.5%）
#   · 產業偏離最大僅 4.4pp → 不是偽裝的產業輪動（這關過了）
#   · Bonferroni(88) p=0.76 不過；Bonferroni(4，視 sbl_pct 為預選) p=0.035 過
