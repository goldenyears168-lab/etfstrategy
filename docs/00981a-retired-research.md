# 00981A 已停損研究線 · 封存說明

> **日期**：2026-06-18  
> **決策**：L1H9 跟單基準已確立 edge；下列研究方向在 OOS／§10 假設檢驗下 **效益不足或方向錯置**，程式已自 repo 移除，歷史報告與 DB 表保留供審計。  
> **仍有效基準**：`L1H9`（T+1 開盤買 · 持有 9 日 · 等權 1 萬）+ **H1 `skip_5_10`**（跳過單日 5–10 檔異動的 rebalance 日，Primary 採納 +3.32pp）。

---

## 1. 停損清單與證據

### 1.1 L1H9 全局 binary skip（§10 Filter Registry）

**問題**：在「經理人已加碼」條件下，再用因子做 **整日／整檔 skip**，多數傷害累計 α 或勝率。

| 假設 | 判決 | Primary 證據 | 摘要 |
|------|------|--------------|------|
| #2 新进-only | 拒絕 | Δ勝率 **−11.1pp** | [`_archive/00981a-filter-studies.md`](../reports/research/_archive/00981a-filter-studies.md) |
| #3 跳空 band skip | 拒絕 | skip 極端 gap 更差 | 同上 |
| #6 TA pattern gate | 拒絕 | skip 過熱 leg **p=0.012** 更差 | 同上 |
| #7 籌碼確認 | 拒絕 | Δ勝率 −8.6pp | 同上 |
| #8 開盤量價（全局 skip） | 拒絕 | 日層不改善；L1+ 延遲 −8.3pp | 同上 |
| #9 加碼力度 top30% | 拒絕 | Δ勝率 −1.2pp | 同上 |
| #4 宏觀 skip（除極端探索） | 拒絕 | 風險日勝率更高 | 同上 |
| R-A / R-B 復檢 | 無採納 | 加權／反向 skip 未過三條件 | 同上 |
| R-COMBO / R-WC | 無採納 | 同 §10.8 | combo / weight_change 報告 |

**方法論**：見 [`00981a-copytrade-research-methodology.md`](00981a-copytrade-research-methodology.md) §10.1–§10.8。

**移除程式**：`copytrade_*` 各 filter 模組、`copytrade_backtest.py` 內 `run_*_filter_study` 系列、`run_00981a_copytrade_backtest.py` 對應 `--compare-*` 旗標。

---

### 1.2 用 v8／v9 行為預測決定跟單子集

**問題**：`P(會加碼)` 與 `P(α | 已加碼)` 是不同問題。

| 證據 | 結果 |
|------|------|
| Copytrade #5 v8 eligible gate | Δ勝率 **−8.2pp**；ineligible leg 勝率更高 |
| OOS 審計 §5 | top_k 命中 H+5 未優於 miss；ineligible 加碼 H+5 更高 |
| OOS v9 vs weight_only | rank +0.3pp，**不顯著** |

**移除程式**：`etf_behavior_predict`、`behavior_*` 全棧、`qlib_tw_factor*` 全棧（2026-06-20）。

**保留**：copytrade 回測（Primary **L1H9**）與 `copytrade/signals` 訊號定義。

**移除程式**：`copytrade_v8_eligible.py`；OOS §4c Track B 建倉評估區塊。

---

### 1.3 Track B 首購（initiation）當 alpha 主線

**問題**：新进 樣本極少；edge 在 repeat **加码**。

| 證據 | 結果 |
|------|------|
| §10 #2 initiation-only | 捕獲 18% 訊號日，α 大降 |
| OOS first cohort | P@K≈0，rank 13% 級別 |
| `behavior_hypothesis_framework` Track B | 樣本不足，不支撐「首購優於 repeat」主線 |

**移除**：initiation filter 研究程式；hypothesis framework 中 Track B 主線輸出。

---

### 1.4 三軌共振（VCP ∩ chunge ∩ p6）當跟單條件

**問題**：選股軌道在「已加碼」條件樣本內辨識力下降。

| 證據 | 結果 |
|------|------|
| §10 #10 triple confluence | Δ勝率 **−5.6pp**；triple leg 勝率低于其他 |

**移除程式**：`copytrade_confluence.py`。

**保留**：p6 / VCP / chunge 作 **universe 選股**（evening brief、VCP daily），不綁跟單。

---

### 1.5 v9 堆更多因子追 OOS rank

**问题**：IncumbentWt（≈ weight_only）已是強基準；再加因子邊際 <0.5pp。

| 證據 | 結果 |
|------|------|
| OOS n=69 | v9 61.3% vs weight_only 61.0% |
| Ablation（v8 窗） | 僅 IncumbentWt 必要；ForeignFlow 可移除 |

**保留**：v9 作 **working model**（daily brief），**不再**投入 primary 因子工程／ablation 擴充。

---

### 1.6 限價／延遲進場「等便宜」

**問題**：動量跟單；成交 leg 為弱勢選擇。

| 證據 | 結果 |
|------|------|
| 限價 −1/−2/−3% | 勝率全面下降；成交 leg p<0.0001 更差 |
| 開盤延遲至 09:15（L1+） | **−8.3pp** |

**移除程式**：`copytrade_limit_entry.py`、`copytrade_opening_confirm.py`。

---

## 2. 未停損（勿混淆）

| 項目 | 角色 |
|------|------|
| **L1H9 矩陣／資金週期** | 基準策略與 H 研究 |
| **H1 `skip_5_10`** | 唯一 Primary 採納的訊號日 filter |
| **等權 vs weight_pct 配置** | 配置研究（非 skip） |
| **leg attribution / regime horizon / event exit** | 歸因與持有期研究 |
| **00981a daily brief（v9）** | 行為監控，非執行 filter |
| **p6 / VCP / chunge 晚報** | 選股軌，獨立於跟單 |

---

## 3. 歷史資料

下列 DB 表 **未刪除**（只讀審計）：

- `copytrade_*_filter_compare`、`copytrade_recheck_compare`、`copytrade_v8_filter_compare` 等
- 封存摘要：[`reports/research/_archive/00981a-filter-studies.md`](../reports/research/_archive/00981a-filter-studies.md)

---

## 4. 相關文件

- 跟單方法論：[`00981a-copytrade-research-methodology.md`](00981a-copytrade-research-methodology.md)
- 行為研究架構：[`00981a-behavior-research.md`](00981a-behavior-research.md)
- OOS 審計：摘要見 [`reports/research/_archive/etf-behavior-studies.md`](../reports/research/_archive/etf-behavior-studies.md)
