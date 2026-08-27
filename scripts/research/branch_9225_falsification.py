#!/usr/bin/env python3
"""9225 凱基內埔 · 單一規格化下單者存在性檢定（可否證設計）。

## 為什麼只剩這一個

第二輪解剖後 9225 是唯一「子群毛邊際 > 全分點」成立的分點（除 9661 外），
且同股同日配對比較贏過包含 9661 在內的所有同儕（Wilcoxon p=1.9e-43）。
但它沒過新判準：純子群過離散度 var/mean=1.65（單一決策源應 <<1）、
整張率 71.2%、標的散在 1,645 檔。

**描述統計已經用盡。缺的是分割檢定。**

## 假設（先寫死，跑之前）

H0：9225 的純當沖子群內部是同質的散戶聚合，
    任何依「下單規格化程度」的切分都不會讓毛邊際分離。
H1：內部藏著一個規格化下單者，其毛邊際明顯高於同子群其餘部分。

## 判準（先寫死）

拒絕 H0 需**同時**滿足：
  (a) 毛邊際差距 > 0.15 個百分點（經濟意義）
  (b) Mann-Whitney p < 0.01 / 檢定數（Bonferroni）
  (c) 規格化那半的過離散度 var/mean 明顯低於另一半
  (d) 差距在兩個獨立時間半段都同向

任一不滿足 → 接受 H0，9225 結案，整條分點找程式的線正式收攤。

## 切分維度（皆為「執行規格化」的代理，與損益無關 → 非循環）
  S1 整張 vs 含零股
  S2 n_lvl（成交價位檔數）低 vs 高 —— 集中執行的指紋
  S3 買賣量完全相等 vs 不等 —— 精確平倉
  S4 單筆張數是否落在該分點的高頻張數集合
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
GAP_MIN = 0.15          # (a) 經濟意義門檻（百分點）
N_TESTS = 4             # 切分維度數，用於 Bonferroni
ALPHA = 0.01 / N_TESTS


def main() -> int:
    d = pd.read_pickle(DIR / "br9225_joined.pkl")
    d = d.dropna(subset=["buy_vwap", "sell_vwap"]).copy()
    d["dt_vol"] = d[["buy_vol", "sell_vol"]].min(axis=1)
    d = d[d.dt_vol > 0]
    d["rt"] = d.dt_vol / d[["buy_vol", "sell_vol"]].max(axis=1)
    d["spread"] = (d.sell_vwap / d.buy_vwap - 1) * 100
    d["noti"] = d.dt_vol * (d.buy_vwap + d.sell_vwap) / 2
    pure = d[d.rt > 0.99].copy()
    print(f"9225 純當沖子群（rt>0.99）：{len(pure):,} stock-day · "
          f"{pure.trade_date.nunique()} 日 · {pure.stock_id.nunique()} 檔")
    print(f"  毛邊際 {(pure.spread * pure.noti).sum() / pure.noti.sum():+.4f}%"
          f"（量加權）／{pure.spread.median():+.4f}%（中位）\n")

    lots_b = pure.buy_vol / 1000.0
    lots_s = pure.sell_vol / 1000.0
    pure["whole"] = (np.isclose(lots_b % 1, 0, atol=1e-6)
                     & np.isclose(lots_s % 1, 0, atol=1e-6))
    pure["exact"] = pure.buy_vol == pure.sell_vol
    lot_round = lots_b.round().astype(int)
    top_lots = set(lot_round.value_counts().head(5).index)
    pure["common_lot"] = lot_round.isin(top_lots)
    nl_med = pure.n_lvl.median()
    pure["few_lvl"] = pure.n_lvl <= nl_med

    def wavg(g):
        return (g.spread * g.noti).sum() / g.noti.sum() if g.noti.sum() else np.nan

    def overdisp(g):
        k = g.groupby("trade_date").size()
        return k.var() / k.mean() if len(k) > 10 and k.mean() > 0 else np.nan

    SPLITS = [("S1 整張", "whole"), ("S2 價位檔數少", "few_lvl"),
              ("S3 精確平倉", "exact"), ("S4 高頻張數", "common_lot")]
    print(f"{'切分':<14}{'規格化組':>10}{'另一組':>10}{'差距':>9}{'p(MW)':>10}"
          f"{'過離散(規格)':>13}{'過離散(另)':>11}{'n(規格)':>9}")
    res = []
    for lab, col in SPLITS:
        a, b = pure[pure[col]], pure[~pure[col]]
        if len(a) < 200 or len(b) < 200:
            print(f"  {lab:<12}樣本不足")
            continue
        ma, mb = wavg(a), wavg(b)
        _, p = stats.mannwhitneyu(a.spread.dropna(), b.spread.dropna(), alternative="two-sided")
        oa, ob = overdisp(a), overdisp(b)
        res.append({"lab": lab, "col": col, "gap": ma - mb, "p": p,
                    "od_a": oa, "od_b": ob, "n_a": len(a)})
        print(f"  {lab:<12}{ma:>+9.4f}%{mb:>+9.4f}%{ma-mb:>+8.4f}{p:>10.2e}"
              f"{oa:>13.2f}{ob:>11.2f}{len(a):>9,}")

    print(f"\n【判準】(a) 差距 >{GAP_MIN} pp　(b) p < {ALPHA:.4f}（Bonferroni {N_TESTS} 檢定）"
          f"　(c) 規格化組過離散更低　(d) 兩個半段同向\n")
    dates = np.sort(pure.trade_date.unique())
    mid = dates[len(dates) // 2]
    verdict = []
    for r in res:
        col = r["col"]
        h1 = pure[pure.trade_date < mid]
        h2 = pure[pure.trade_date >= mid]
        g1 = wavg(h1[h1[col]]) - wavg(h1[~h1[col]])
        g2 = wavg(h2[h2[col]]) - wavg(h2[~h2[col]])
        ok_a = abs(r["gap"]) > GAP_MIN
        ok_b = r["p"] < ALPHA
        ok_c = pd.notna(r["od_a"]) and pd.notna(r["od_b"]) and r["od_a"] < r["od_b"]
        ok_d = np.sign(g1) == np.sign(g2) and abs(g1) > 0.05 and abs(g2) > 0.05
        allok = ok_a and ok_b and ok_c and ok_d
        verdict.append(allok)
        print(f"  {r['lab']:<12}(a){'✓' if ok_a else '✗'} (b){'✓' if ok_b else '✗'} "
              f"(c){'✓' if ok_c else '✗'} (d){'✓' if ok_d else '✗'}"
              f"　前半 {g1:+.4f} 後半 {g2:+.4f}　→ {'★ 拒絕 H0' if allok else '接受 H0'}")

    print(f"\n{'='*70}")
    if any(verdict):
        print("★ 至少一個切分拒絕 H0 —— 9225 內部可能藏著規格化下單者，值得再追。")
    else:
        print("✗ 四個切分全部接受 H0 —— 9225 內部是同質的散戶聚合。")
        print("  → 9225 結案。分點層級找程式的研究線正式收攤。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
