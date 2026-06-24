---
page_id: research_case_rrg_mono_swap_accel
layer_id: research
research_topic: rrg-mono-score-swap-c
graduated_strategy_id: rrg-mono-swap-accel
title: 採納報告 · RRG 四日加速換倉
tab_label_zh: 採納 · RRG 換倉
tab_label_en: Adoption · RRG swap-accel
sort_order: 33
role: graduation · mode C score swap · breadth hold-out
web_v1: 採納報告
---

# 採納報告 · RRG 四日加速換倉（rrg-mono-swap-accel）

← [策略目錄](strategy_catalog) · [凍結規格](strategy_rrg_mono_swap_accel) · 母策略 [RRG 市場輪動圖選股策略](strategy_rrg_mono_hold7) · [RRG 持7日採納報告](research_case_rrg_mono)

**研究主題** · `rrg-mono-score-swap-c` · 在 **RRG mono** fresh 池上，用 **四日平均加速度（avg acceleration）** 做對称换仓，能否優於純 **hold7**？

> **先讀這裡**：這不是另一條輪動宇宙，而是在已採納的 **RRG 市場輪動圖選股策略（持7日）** 候選池上，把「買了就不動 7 日」改成 **盤中 C0 進場 + 5 分鐘輪詢換倉**——賣還在變弱的那條腿，買轉強最快的那條 challenger。

---

## 1 · 研究問題

白話問句：**同一批 fresh mono 候選裡，用四日加速對称换仓，能否比固定持有 7 日賺更多超額？**

| 維度 | hold7（母策略） | 本策略（採納） |
|------|----------------|----------------|
| 候選池 | fresh mono · 依軌跡排序 Top10 | fresh mono · **全池**（不裁 top10） |
| 進場 | 第 4 日**收盤** | **C0** 盤中 scale（confirm 0\|1） |
| 出場／換倉 | +7 日收盤賣 | **5m poll** · min 5 / max 10 交易日 · max 1 次/日 |
| 槽位 | 3 | 3 |
| 資金模型 | 5 萬等分 | **6 萬**等分（回測 SSOT） |

---

## 2 · 簡化漏斗（SSOT）

漏斗消融（`20260624_c18acc_funnel_ablation.json`）確認下列 **三塊** 即冠軍完整規格；拿掉 no-op 層 **不變** 5.38% / 41 swaps。

| 區塊 | 規則 |
|------|------|
| **A 池** | mono tier2（up_right + leading + disp∈[1,2) + mono_up）→ fresh 新進 → seg_last 全池 |
| **B 進** | C0 盤中 scale · 5m · confirm_bars 0 或 1 |
| **C 換** | 賣四日加速最負且 <0 · 買 seg_last+0.05 後加速最大 · min5 max10 · 1 swap/日 |

**已拿掉（no-op）**：top10 裁切 · shortlist 再篩（accel>0 · challenger_gate）。

**不可簡化**：mono_up / leading / disp / fresh · 買方加速排序（−0.07pp）· max_swaps=1。

---

## 3 · 探索路徑（摘要）

1. **盤中進場（C0）** 優於收盤 full_rrg 進場（intraday entry sweep）。  
2. **模式 C score swap** 優於純 hold7 與較早的 close-swap 骨架（C13 系）。  
3. **賣**：四日平均加速最負，且加速仍為負（`accel_sell_negative_only`）。  
4. **買**：challenger 的 `seg_last` 須高於持倉 + **margin 0.05**，再取 **四日加速最大**（合併單條規則 · 非兩步）。  
5. **候選池**：維持 **fresh mono 全池**；放寬 mono_tier2／mono_up 全池或拿掉 mono_up／leading 會 **稀釋換倉品質**（候選池對照 · funnel ablation）。

<details>
<summary><strong>全樣本對照（2024-01-01～2026-06-22 · 每筆均超額%）</strong></summary>

| 變體 | 均超額% | 換倉次數 | 結論 |
|------|---------|----------|------|
| **C18acc（採納）** | **+5.38** | 41 | **採納** · 簡化漏斗 SSOT |
| C18acc-mono（mono tier2 全池） | +5.53 | 46 | **拒絕** · 2025 分年回撤 |
| C18acc-entry-sigseg（無 scale 進場） | +5.49 | 41 | **研究備選** · 未替換冠軍 |
| C18-dls1（穩定對照） | +5.19 | 33 | 保留研究對照 · 分年互有勝負 |
| C18 骨架 baseline | +4.89 | 45 | 內部骨架 · 非對外名稱 |
| mono_up fresh 池 | +4.45 | 65 | **拒絕** · churn 高 |
| 無 leading 加速篩 | ~+3.65～3.82 | 70+ | **拒絕** |

</details>

---

## 4 · Market breadth（市場廣度）hold-out

**閘門**：強勢 + 過熱區間的 **均超額均 > 0**（2024-01～2026-06-22）。

| 200MA 區間 | 結論 |
|-----------|------|
| **強勢** | 通過 · 樣本偏薄 |
| **過熱** | 通過 |
| 超賣／偏弱／中性 | 樣本不足 · 不單獨採納 |

→ 與母策略類似，實盤成交多落在 **強勢／過熱**；完整分桶見研究 JSON `20260624_rrg_mono_swap_accel_breadth_zones.*`。

---

## 5 · 分年組合績效（6 萬 · 3 槽 · 日內市值計價）

與策略目錄比較表同源 · `strategy_performance_yearly` · 窗口 **2026 = 2026-01-01～06-18**。

| 年份 | 窗口 | 組合總報酬% | 年化報酬率% | 勝率% | Sharpe | 樣本 |
|------|------|------------|------------|---------|--------|------|
| **2025** | 2025-01-01～12-31 | **+133.2** | **+140.7** | 70.1 | 3.22 | 77 筆 |
| **2026** | 2026-01-01～06-18 | **+173.0** | **+919.2** | 65.7 | 7.37 | 35 筆 |

| 指標 | 全樣本回測窗口 | 備註 |
|------|----------------|------|
| 每筆均超額% | **+5.38%** | 183 筆 · 41 次換倉腿 |
| 對照基準 | 台灣加權指數 | 與目錄表一致 |

2026 年化為部分年度外推 · **不可**與 2025 全年直接排名 · 亦**不可**與母策略 5 萬本金列直接比「誰賺比較多」。

---

## 6 · 為何採納（相對 hold7）

1. **超額結構**：全樣本每筆均超額 **+5.38%** · 優於同池 hold7 的短波段固定出場邏輯。  
2. **廣度 hold-out 通過**：強勢／過熱區間均未出現系統性負超額。  
3. **候選池紀律**：funnel ablation 支持 **fresh mono 全池**；放寬 mono_tier2／mono_up 未帶來更穩健的 graduation。  
4. **執行分工**：**16:30 收盤診斷**（Scheme A · 不下單）+ **盤中 5m poll screen** · 與母策略日頻掃描錯開。

## 7 · 為何尚未全自動啟用

- `enabled: false`：規格已 **採納凍結** · 盤中 screen 預設 dry-run · 待手動確認後啟用 production poll。

---

## 8 · 採納摘要

| 研究 | → 策略 |
|------|--------|
| fresh mono 全池 · 四日加速卖弱买强 · C0 scale + 5m poll | [RRG 四日加速換倉](strategy_rrg_mono_swap_accel) |
| 收盤診斷 | `rrg_mono_swap_accel_daily` · 16:30 |
| 盤中執行 | `rrg_c18acc_screen` · 09:00–13:20 |
