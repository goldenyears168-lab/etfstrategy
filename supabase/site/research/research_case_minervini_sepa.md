---
page_id: research_case_minervini_sepa
layer_id: research
research_topic: broad-momentum-sepa
graduated_strategy_id: minervini-sepa-basket
title: 示範 · Minervini SEPA 研究
tab_label_zh: 案例 · Minervini SEPA
tab_label_en: 案例 · Minervini SEPA
sort_order: 34
role: TV strategy comparison
web_v1: 研究示範
---

# 示範案例 · Minervini SEPA 趨勢籃研究

← [策略目錄](strategy_catalog) · [凍結規格](strategy_minervini_sepa_basket)

**研究主題** · 廣度動量 · Minervini SEPA 對照實驗

> **先讀這裡**：這不是日內「槽位輪動」策略，而是 **月末再平衡** 的持倉籃：每月底用 **Minervini Trend Template（趨勢模板）** 挑出符合 **Weinstein Stage 2（第 2 階段）** 的強勢股，等權持有。研究在比：這條規則是否優於其他常見動量／趨勢對照。

---

## 1 · 研究問題

在約 133 檔 ETF 成分股、對照基準為台指的前提下：**月末等權 Stage 2 籃（Trend Template 7/8 項通過）** 是否優於其他 TradingView 映射的動量規則？它與日內槽位型的 VCP／RRG／Copytrade（跟單）**資金模型不同**，適合作並行持倉軌。

---

## 2 · 對照實驗設計

| 對照 | 策略 | 類型 |
|------|------|------|
| 買入持有 | 台指 Buy & Hold | 基準 |
| 12月絕對動量 | **Antonacci** 12 月絕對（>無風險） | 月頻動量 |
| 12月正報酬 | 12 月報酬 (>0) | 月頻動量 |
| **Minervini 趨勢籃** | **Minervini SEPA** | **候選** |
| ADX-RSI 趨勢 | NADY ADX-RSI Trend | 趨勢過濾 |

### 方法約束

- **訊號日僅用當日及以前資料（PIT）**：僅用月末及以前資料 · 次月首交易日再平衡  
- **報酬上限** = 35%（單月 clip）  
- **廣度濾網**：50MA 上方 ≥55% 且 200MA 上方 ≥45%（研究標籤：強勢）  
- **空倉**：無合格標的 → 現金  

---

## 3 · 回測結果（2024-01-01～2026-06-18）

| 指標 | Minervini SEPA 趨勢籃 |
|------|------------------------|
| **總報酬率%** | **477.27** |
| **區間超額報酬%** | **318.13** |
| **Sharpe** | **2.51** |
| **最大回撤%** | **−28.61** |

**採納邏輯**：長窗超額 / Sharpe 優於或互補 TV 對照軌 · 與日內槽位策略資金模型不同 · 適合作 **持倉型** 並行軌。

---

## 4 · 延伸探索（尚未採納為策略）

| 方向 | 問題 |
|------|------|
| 外部動能 vs ETF00981A 跟單策略 | FinPilot 等外部策略勝率比較 |
| 社群策略 vs 隔日開盤 | 社群策略 vs 跟單基準 |
| S04 因子分層 / 動量 | 因子分層與動量掃描 |

→ 結果仍屬探索 · **未** 寫入凍結規格。

---

## 5 · 採納摘要

| 研究 | → 策略 |
|------|--------|
| 趨勢模板 7/8 · 月頻 · Stage 2 | [Minervini SEPA](strategy_minervini_sepa_basket) |
| 月末再平衡 · 非日內槽位 | 持倉型並行軌 |
