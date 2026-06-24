---
page_id: strategy_rrg_mono_hold7
layer_id: strategy
strategy_id: rrg-mono-hold7
title: RRG 市場輪動圖選股策略（持7日）
tab_label_zh: RRG 市場輪動圖選股策略
tab_label_en: RRG 市場輪動圖選股策略
sort_order: 12
role: 已採納凍結規格 · RRG 輪動
web_v1: 策略獨立頁
icon: ri-bubble-chart-line
description_short: 從 Relative Rotation Graph（RRG）找 fresh 轉強標的 · 持 7 日 · 3 槽 · 日頻收盤掃描
research_page_id: research_case_rrg_mono
brief_types:
  - rrg_mono_daily
  - rrg_mono_intraday
---

# RRG 市場輪動圖選股策略（持7日）

← [策略目錄](strategy_catalog) · [採納報告](research_case_rrg_mono) · 變體 [RRG 四日加速換倉](strategy_rrg_mono_swap_accel)

**節奏** · 日頻（收盤後）· 對照 [市場環境日報](/)

## 策略定義

ETF 成分股中，**相對強度輪動（RRG）** 單軌 **fresh** 四日軌跡 · **依軌跡排序** → 第 4 日收盤買 → **7 個交易日** 後收盤賣 · **3 槽**（5 萬等分）· 基準 **台指**。

## 採納理由

1. **RRG 輪動** 框架下之相對強度延續假說。  
2. 參數格採納：回看 **4 日** · 持有 **7 日** · **3 槽**。  
3. 2026 上半年均超額 ~**+7%**／筆 · 作 VCP 掃描 **RRG 對照基準**。  
4. 2026 成交多落 **強勢／過熱** 廣度（見 [廣度分層](research_case_rrg_mono#廣度分區分層)）。

## 規格狀態

- 2026 上半年回測 **39 筆**
- 廣度分區見 [研究案例](research_case_rrg_mono#廣度分區分層)

## 規則摘要

| 項目 | 值 |
|------|-----|
| 宇宙 | ETF 成分股 |
| 訊號 | RRG 單軌 fresh · 回看 4 日 |
| 排序 | 依軌跡末段 |
| 進／出 | 第 4 日收盤進 · +7 日收盤出 |
| 資金 | 3 槽 · 50,000 NTD |

## 績效與風險

### 分年績效（5 萬 · 3 槽 · 日內市值計價）

| 年份 | 窗口 | 組合總報酬% | 年化報酬率% | 勝率% | Sharpe | 樣本 |
|------|------|------------|------------|---------|--------|------|
| **2025** | 2025-01-01～12-31 | **+95.0** | **+99.9** | 51.7 | **2.87** | 89 筆 |
| **2026** | 2026-01-01～06-18 | **+129.2** | **+580.4** | 55.0 | **6.31** | 40 筆 |

| 指標 | 2026 上半年 | 備註 |
|------|-------------|------|
| 每筆均超額% | **+6.5%** | 40 筆 vs 台指 |
| 組合回撤 | 待公布 | [風險快照](strategy_catalog#風險快照) |

2026 年化為部分年度外推 · 五軌對照 [績效對照](strategy_catalog#績效對照)。

## 凍結規格

| 項目 | 值 |
|------|-----|
| RRG 週期／回看 | 20／4 日 |
| 候選數／槽位／持有 | 10／3／7 日 |
| 進出場 | 收盤價 |

## 研究出處

[RRG 研究案例](research_case_rrg_mono) · 換倉變體見 [四日加速換倉採納報告](research_case_rrg_mono_swap_accel)
