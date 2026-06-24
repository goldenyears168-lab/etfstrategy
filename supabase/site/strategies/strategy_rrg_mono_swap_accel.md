---
page_id: strategy_rrg_mono_swap_accel
layer_id: strategy
strategy_id: rrg-mono-swap-accel
title: RRG 四日加速換倉
tab_label_zh: RRG 四日加速換倉
tab_label_en: RRG swap-accel
sort_order: 13
role: 已採納凍結規格 · RRG 輪動 · 四日加速换仓
web_v1: 策略獨立頁
icon: ri-exchange-line
description_short: fresh mono 全池 · 四日加速卖弱买强 · C0 盘中 scale + 5m poll · 3 槽
research_page_id: research_case_rrg_mono_swap_accel
brief_types:
  - rrg_mono_swap_accel_daily
  - rrg_c18acc_screen
---

# RRG 四日加速換倉

← [策略目錄](strategy_catalog) · [採納報告](research_case_rrg_mono_swap_accel) · 母策略 [RRG 市場輪動圖選股策略（持7日）](strategy_rrg_mono_hold7)

**節奏** · 16:30 收盤診斷（Scheme A）· 盤中 live screen · 對照 [市場環境日報](/)

## 策略定義

在 **RRG mono fresh** 候選池上，以 **四日平均加速度（avg acceleration）** 對称换仓。**C0** 盤中 scale 進場 · **5 分鐘**輪詢換倉 · **3 槽** · 最少持有 **5**／最多 **10** 交易日。

### 簡化漏斗（SSOT · 20260624 funnel ablation）

**A · 候選池**（PIT · 信號日 T−1 收盤）

1. ETF 成分 watchlist  
2. **mono tier2**：up_right + 終點 leading + disp∈[1,2) + mono_up  
3. **fresh**：昨日非 mono tier2、今日新進 mono tier2  
4. 依 **seg_last** 排序 · **全池**（不裁 top10；池日均約 1 檔 · 2024–2026 max=8）

**B · 進場**（空槽補倉）

- **C0** 盤中 · 5m · 信號日 seg_last 以盤中價 **scale** 排序  
- `confirm_bars` **0 或 1** 皆可（消融換倉腿相同）

**C · 換倉**（5m poll · 每日最多 1 次）

- **賣**：四日平均加速（lookback=4）最負，且加速 **< 0**  
- **買**：`seg_last` 須高於持倉 **+0.05**，再取 **四日加速最大**（合併單條規則）

已拿掉且不影響回測：top10 裁切 · shortlist 再篩（accel>0 · challenger_gate · top-N 重排）。

## 採納理由

1. 全樣本（2024-01～2026-06）每筆均超額 **+5.38%** · **41** 次換倉腿 · 優於同池固定 hold7。  
2. **Market breadth（市場廣度）** hold-out 已通過（強勢＋過熱均超額 > 0）。  
3. 候選池對照與 funnel ablation 支持維持 **fresh mono**；放寬 mono_up／無 leading 未通過 graduation。  
4. **16:30 診斷 + 盤中 poll** 分工：收盤只看隔日候選與換倉門檻 · 執行在交易時段。

## 規格狀態

- `enabled: false`（採納凍結 · 待手動啟用 production screen）
- 內部回測代號：`C18acc` · 詳見 [採納報告](research_case_rrg_mono_swap_accel)

## 規則摘要

| 項目 | 值 |
|------|-----|
| 宇宙 | ETF 成分 · fresh mono · **全池**（mono tier2 + 今日新進） |
| 賣 | 四日平均加速 · 僅加速 < 0 |
| 買 | seg_last > 持倉 + 0.05 · 四日加速最大（單條合併規則） |
| 進場 | C0 · 盤中 scale · confirm 0\|1 |
| 換倉 | poll 5m · ≥09:30 可換 · max 1 次/日 |
| 持有 | min 5 · max 10 交易日 · 3 槽 |

## 績效與風險

### 分年績效（6 萬 · 3 槽 · 日內市值計價）

| 年份 | 窗口 | 組合總報酬% | 年化報酬率% | 勝率% | Sharpe | 樣本 |
|------|------|------------|------------|---------|--------|------|
| **2025** | 2025-01-01～12-31 | **+133.2** | **+140.7** | 70.1 | 3.22 | 77 筆 |
| **2026** | 2026-01-01～06-18 | **+173.0** | **+919.2** | 65.7 | 7.37 | 35 筆 |

2026 年化為部分年度外推 · 本金與母策略（5 萬）不同 · **不可**直接比較總報酬排名。

## 研究出處

[採納報告](research_case_rrg_mono_swap_accel) · topic `rrg-mono-score-swap-c`
