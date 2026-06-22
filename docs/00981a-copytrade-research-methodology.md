# 00981A Copytrade 回測 · 方法論與關鍵發現

> **讀者**：延續 copytrade 矩陣研究的決策者與實作者。  
> **已停損研究**（filter／v8 gate／首購主線等）：[`00981a-retired-research.md`](00981a-retired-research.md) — §10 程式已自 repo 移除，歷史報告保留。  
> **資料庫 batch**：`00981a-copytrade-h20-20260617`（L×H 矩陣）、`00981a-allocation-compare-l1h9-20260617`（配置對照）  
> **程式**：`src/copytrade_backtest.py`、`scripts/run_00981a_copytrade_backtest.py`  
> **窗口**：約 2025-05-28 ～ 2026-06-17（242 訊號日，241～233 有效 complete 日視 H 而定）

---

## 1. 研究問題演進

| 階段 | 問題 | 產出 |
|------|------|------|
| A | 00981A 新进/加码 跟單，不同進場延遲 × 持有天數哪個賺？ | L×H 矩陣（100 格，H1–H20） |
| B | 相對台指有沒有超額？顯著性隨 H 如何 decay？ | `copytrade_horizon_decay`、α 矩陣 |
| C | 總資金有限，持有幾天賣出再輪動最划算？ | `copytrade_capital_cycle`、Optimal hold (H*) **H9** |
| D | 跟單 vs 直接買 00981A ETF？ | `copytrade_etf_compare` · `--analyze-etf-compare` |
| E | 等權 1 萬 vs 依經理人 weight_pct 配置？ | `copytrade_allocation_compare` |

---

## 2. 訊號與回測規則

### 2.1 訊號定義

- **訊號日 T**：ETF 持股快照公布日（收盤後可比較前後快照）。
- **觸發條件**：`compute_etf_holdings_changes` 中 action 為 **新进** 或 **加码**，且 `share_delta > 0`。
- **執行標的**：當日所有觸發 leg 的**成分股**（不是買 ETF 本身）。

### 2.2 進場列 L（何時買）

| 代號 | 含義 | 可執行？ |
|------|------|----------|
| **L0O** | T 當日**開盤**買 | 否（開盤尚不知持股，oracle／lookahead） |
| **L0C** | T 當日**收盤**買 | 否（當日收盤才知持股） |
| **L1** | **T+1 開盤**買 | **是（實盤基準）** |
| **L2** | T+2 開盤買 | 是 |
| **L3** | T+3 開盤買 | 是 |

### 2.3 持有 H（持幾天賣）

- **H** = 自進場日起持有 **H 個交易日**，第 H 日**收盤**賣出。
- 例：L1H9 = T+1 開盤買 → 持有 9 交易日 → 收盤賣。

### 2.4 資金配置（預設）

- 每個**訊號日**部署 **10,000 NTD**（在該日所有新进/加码 leg 間分配）。
- **等權（equal）**：當日 N 檔 → 每檔 `10,000 / N`。
- **按比例（weight_pct）**：依當日快照 `weight_pct_curr` 在當日 leg 間比例分配（單檔訊號日與等權相同）。

### 2.5 基準與成本

- **台指基準**：IX0001，與組合同 **進場價模式**（開盤／收盤）及 **進出日期** 計算基準報酬。
- **成本**：預設 0 bps（未扣手續／稅／滑價）。

---

## 3. α（Alpha）的定義

報告中的 **α** 不是「贏 00981A ETF」，而是：

```
單筆 α (NTD) = 跟單組合損益 − 同期台指損益（同進出規則、同部署金額）
日均超額%     = 單筆 (組合報酬% − 台指報酬%)
累計 α       = 所有 complete 訊號日的 α 加總
```

**Gross 損益** = 跟單組合實際賺賠，**含大盤 beta**。  
長 H 時 Gross 常遠大於 α，代表獲利多半來自大盤，非選股超額。

另存 **CAPM α**（`total_capm_alpha_ntd`）：基準改為 `組合 beta × 台指報酬`，本報告解讀以簡易 α 為主。

---

## 4. 統計檢定方法

### 4.1 顯著性 Decay（`copytrade_horizon_decay`）

對每個 **(L, H)** 策略：

1. 收集所有 complete 日的 **日均超額%** = `return_pct − bench_return_pct`。
2. **單樣本 t 檢定**（對 0）與 **Wilcoxon 符號秩檢定**（對 0）。
3. 解讀：
   - **首次顯著 H**：第一個 Wilcoxon **p < 0.05** 的 H（L1 為 **H5**）。
   - **末次仍顯著**：最後一個 p < 0.05 的 H（L1 為 **H20**）。
4. **注意**：H1–H20 多重檢定**未** Bonferroni 校正；以 decay **趨勢**為主，非單點 p 值。

### 4.2 有限資金週轉（`copytrade_capital_cycle`）

假設 **單一資金池 10,000 NTD**：

```
若上一筆尚未 exit_date 釋放，則跳過新訊號（不接重疊單）
否則以該 H 的損益接入下一筆可執行訊號
```

指標：

| 指標 | 意義 |
|------|------|
| `recycled_total_alpha_ntd` | 單池輪動累計 α |
| `recycled_n_cycles` | 實際成交筆數 |
| `signal_capture_pct` | 成交筆數 / 總訊號日（00981A 訊號密，H 愈長捕獲愈低） |
| `alpha_per_locked_day` | 實現超額 / 鎖倉總交易日 |

**Optimal hold (H\*)**：使 `recycled_total_alpha_ntd` 最大的 H（L1 為 **H9**）。
**邊際遞減**：H* 之後，延長持有使 `marginal_recycled_alpha_ntd` 明顯下降（L1：H10 起轉負）。

### 4.2.1 固定槽位（`copytrade_capital_slots` · `--analyze-fixed-slots`）

固定總本金 **C = n_slots × per_signal**（例：9 槽 × 1 萬 = 9 萬）：

```
最多 n_slots 筆同時持倉；exit 日收盤釋放，同日不可接新 entry（與单池相同）
槽滿則跳過訊號
```

| 參數 | 說明 |
|------|------|
| `--capital` | 總本金（與 `--slots` 搭配） |
| `--slots` | 槽位數（預設 `floor(capital/per_signal)`） |
| `--per-signal` | 每訊號部署（預設 10,000） |
| `slots_mode=fixed` | 固定槽數 |
| `slots_mode=match_horizon` | 每個 H 用 H 槽（全捕獲對照） |

**00981A 樣本（batch `00981a-copytrade-h20-20260617`）**：

| 模型 | H* (optimal hold) | 實現超額 | 捕獲率 | 備註 |
|------|--------|--------|--------|------|
| 单池 1 万 | **9** | +11,043 | 11.6% | 同一筆錢輪動 |
| 固定 9 槽 / 9 万 | **20** | +64,561 | 48.0% | H9 可 100% 捕獲、+39,810 |
| 槽 = H | 20 | +111,463 | ~100% | 等同无约束 |

→ **9 萬九槽**：要全接訊號用 **H9**；要極大化總實現超額 可 **H20** 但漏半數訊號。詳見 `reports/research/00981a-copytrade/20260618_00981a_horizon_fixed_capital.md`。

### 4.2.2 Trend posture stratification（轨 A · `copytrade_regime_horizon`）

```bash
PYTHONPATH=src .venv/bin/python scripts/run_00981a_copytrade_backtest.py \
  --analyze-regime-horizon --write-report \
  --batch-id 00981a-copytrade-l1h45-20260618
```

PIT 标签：讯号日 T 的 `trend_posture`（台指 trend）+ `exposure_decision`（trend_posture_score 55% + top_risk 45%）。

**00981A 样本（2025-05～2026-06）**：见 `reports/research/00981a-copytrade/20260618_00981a_regime_horizon_l1.md`。

| 发现 | 说明 |
|------|------|
| 弱市未现短持 | contraction（n=13）/ cash-priority 仍长 H 累计 α 最高 |
| 主样本在 broadening | 193/233 日；长持优势主要来自牛市 regime |
| 实务拐点仍在 H20 附近 | broadening 桶 H20 后边际仍正但递减速 |

### 4.2.3 Leg 级 α 衰减（轨 B · `copytrade_leg_decay`）

```bash
PYTHONPATH=src .venv/bin/python scripts/run_00981a_copytrade_backtest.py \
  --analyze-leg-decay --write-report \
  --max-hold 45 --per-signal 10000
```

- **单位**：每个新进/加码 **leg**（非讯号日组合）。
- **进场**：L1（T+1 开盘）；出场：进场后第 H 日收盘。
- **分层**：`all` · `新进/加码` · `single_leg/multi_leg`。
- **膝点**：`mean_excess%` 达峰后边际日均超额跌破阈值 → 建议最短持有参考。

报告：`reports/research/00981a-copytrade/*_00981a_leg_horizon_decay.md`。

**00981A 样本（2025-05～2026-06，1866 legs）**：

| 发现 | 说明 |
|------|------|
| 加码主导 | 1817/1866 legs；新进仅 49，分层结论以加码为准 |
| 边际膝点 H27 | 加码桶 Δsum α 自 H28 起连续低于峰值 25%（H27 仍 +82k） |
| α/日效率峰 H27 | `sum_α / H` 在 H27 达峰；H20 仍保留 86% 效率、边际 +60k |
| H28–29 平台 | mean 超额仍升，但 H28 Δmean 仅 +0.05pp、H29 Δsum α 转负 |
| 新进样本过小 | n=33–49，膝点 H14 不稳定，勿单独定规 |

### 4.2.4 事件驱动出场（轨 C · `copytrade_event_exit`）

```bash
PYTHONPATH=src .venv/bin/python scripts/run_00981a_copytrade_backtest.py \
  --analyze-event-exit --write-report \
  --baseline-h 20 --capital 100000 --per-signal 10000
```

在 **H20 基准**上叠加出场触发器；对照 **10 万 rotation**（H20 槽）實現超額。

| 政策 | 规则 |
|------|------|
| `exit_reduce_clear` | 同股经理减码/出清 → T+1 开盘卖 |
| `exit_regime_restrictive` | 持仓中 exposure→restrictive/cash → T+1 开盘卖 |
| `exit_reduce_or_regime` | 以上取先到者 |
| `readd_extend_h20` | 持仓中再次加码 → 自加码日延长 H20 |

报告：`reports/research/00981a-copytrade/*_00981a_event_exit_l1h20.md`。

**00981A 样本（H20 基准 · 1866 legs · rotation 10 万）**：

| 政策 | 判决 | total Δα | rotation Δα | 说明 |
|------|------|----------|-------------|------|
| 减码/出清提前卖 | **拒绝** | −124k | +3.1k | leg 配对 p=0.037；牛市中卖太早 |
| regime 转弱提前卖 | **拒绝** | −567k | +3.7k | 95% leg 触发；restrictive 在牛市持仓期过频 |
| 再次加码延长 H20 | **探索** | +538k | +18.0k | 延长非提前卖；rotation Primary 改善 |

→ **维持固定 H20 默认**；可研究「加码续持」作为轨 D 条件规则，不采纳减码/regime 提前出场。

### 4.2.5 Leg 动量归因（gap × p5d · `copytrade_leg_attribution`）

```bash
PYTHONPATH=src .venv/bin/python scripts/run_00981a_copytrade_backtest.py \
  --analyze-leg-attribution --write-report \
  --strategy-id L1H9
```

- **gap**：T 收 → T+1 开 overnight gap
- **p5d**：讯号日前 5 日涨幅
- **假说**：H-G1 深 gap · H-G2 高 p5d · H-G4 交互 · 个案 2026-03-06 vs 03-12

报告：`reports/research/00981a-copytrade/*_00981a_leg_attribution_l1h9.md`。

**00981A L1H9 全样本（1845 legs）**：

| 假说 | 判决 | 要点 |
|------|------|------|
| H-G1 深 gap (<−6%) | **support** | mean超额 +19.8% vs +1.5% · p=0.005 |
| H-G2 高 p5d (≥8%) | inconclusive | 全样本未显著更差；个案 6510 仍成立 |
| H-G4 深gap+低p5 | **support** | vs 浅gap+高p5 · p<0.0001 |
| H-G5 skip_overextended | **support** | 过热标签 leg 更差 · 仍不采全局 skip |

→ 轨 D 候选：**深 gap 加权**；**高 p5d 单因子 insufficient**，需与 gap 交互。

### 4.3 配置對照（`copytrade_allocation_compare`）

固定 L1H9，僅改配置方式：

- 逐日配對 **weight_pct − equal** 的報酬差、α 差。
- **Wilcoxon** 檢定配對差異是否顯著 ≠ 0。

### 4.4 跟單 vs 買 00981A（`copytrade_etf_compare` · `--analyze-etf-compare`）

非 α 框架：在 **相同 entry_date / exit_date** 下，比較：

- 跟單成分股組合 gross，vs
- 同期買賣 **00981A** 的 gross（T+1 開盤進 · H 收盤出 · 同 deploy）。

```bash
PYTHONPATH=src .venv/bin/python scripts/run_00981a_copytrade_backtest.py \
  --analyze-etf-compare --write-report \
  --strategy-id L1H20 --capital 100000 --etf-slots-mode rotation
```

| 參數 | 說明 |
|------|------|
| `--strategy-id` | 預設 L1H9；Primary 建議 **L1H20** |
| `--capital` | rotation 總本金（預設 100,000） |
| `--etf-slots-mode` | `rotation`（H 槽 · deploy=capital/H）或 `unconstrained` |

**00981A 樣本（L1H20 · 100k rotation · 5k/訊號）**：

| 檢定 | 結果 |
|------|------|
| 配對 n | 223 |
| 勝率 CT>ETF | **55.16%** |
| 均超額 | **+1.41 pp** |
| Wilcoxon p | **0.040** |
| 累計 gross 差 | **+15,670 NTD** |
| 判決 | **support** |

→ **H20 rotation 在訊號日層面顯著優於同期買 ETF**；**H9 不顯著**（p≈0.53）。  
→ **買入持有 ETF** 在本段牛市總 gross 仍可能高於 rotation（全程暴露 vs 訊號輪動）。  
報告：`reports/research/00981a-copytrade/*_00981a_etf_compare_l1h20.md`。

---

## 5. 關鍵發現摘要

### 5.1 L1 顯著性 Decay（無限資金、每訊號 1 萬）

| H 區間 | Wilcoxon vs 台指 | 累計 α 趨勢（L1） |
|--------|------------------|-------------------|
| H1–H4 | **不顯著**（p > 0.05） | 低 |
| H5 起 | **顯著** | 上升 |
| H20 | 顯著 | 峰值 +111,463 NTD |

→ **統計上**至少持有 **5 天**才穩定贏台指；**有限資金下**總實現超額仍以 **H9** 最佳。

### 5.2 有限資金 Optimal hold (H*)（單池 1 萬）

| 進場 | H* | 單池實現超額 | 成交筆數 |
|------|--------|-----------|------|
| **L1 T+1** | **9** | **+11,043** | 27 |
| L2 T+2 | 9 | +9,994 | 27 |
| L3 T+3 | 18 | +9,084 | 14 |
| L0O（不可執行） | 1 | +31,002 | 242 |

**實盤建議**：T 收盤後偵測 → **T+1 開盤買** → **持有 9 交易日收盤賣** → 釋放後接下一可執行訊號。

### 5.3 兩種資金模型（易混淆）

| 模型 | 需要資金 | 行為 | 契約角色 |
|------|----------|------|----------|
| **無約束累計** | **約 H × 1 萬**（H9 ≈ **9 萬**） | 每日訊號都配，持倉重疊 | **§10 Primary**（`total_alpha_ntd`） |
| **單池輪動** | **約 1 萬** | 同一筆輪流用，重疊訊號跳過 | **§10 Secondary**（`recycled_total_alpha_ntd`）· **§4.2 Optimal hold (H*)** |

### 5.4 L0O 漂亮 vs L1 贏 ETF（不矛盾）

- **L0O**：時間 oracle + H1 極快輪動 → 回測上限。
- **L1 vs ETF**：同日期下 **買新进/加码 成分股** vs **買整檔 ETF**；單池 L1H9 gross +18,859 vs ETF +11,807（**+7,052**），幅度有限且未扣成本。

### 5.5 等權 vs weight_pct（L1H9）

| 指標 | 等權 | 按 weight_pct | 配對 p(W) |
|------|------|---------------|-----------|
| 累計 α | +39,810 | +37,360 | — |
| 單池實現超額 | +11,043 | +10,537 | — |
| 日均報酬差 | — | — | **0.902（不顯著）** |

→ **維持等權即可**；87% 訊號日為多檔，但權重差異未帶來顯著超額。

---

## 6. SQLite 產物一覽

| 表 | 內容 |
|----|------|
| `copytrade_runs` | 每策略 run 摘要（100 格矩陣） |
| `copytrade_signal_days` | 逐訊號日損益 |
| `copytrade_legs` | 逐檔明細 |
| `copytrade_horizon_decay` | L×H 顯著性 decay |
| `copytrade_capital_cycle` | 單池輪動各 H |
| `copytrade_allocation_compare` | 等權 vs weight_pct |
| `copytrade_etf_compare` | 跟單 vs 買 ETF（配對檢定） |
| `copytrade_gap_filter_compare` | 隔夜跳空篩選 |
| `copytrade_macro_filter_compare` | 大盤期貨隔夜風控 |
| `copytrade_ta_filter_compare` | TA Pattern Gate |
| `copytrade_chip_filter_compare` | 籌碼確認 |
| `copytrade_opening_filter_compare` | 開盤量價確認 |
| `copytrade_limit_entry_compare` | 限價進場執行 |
| `copytrade_leg_conviction_snapshots` | 加碼力度分位 |
| `copytrade_conviction_filter_compare` | 力度分位篩選 |
| `copytrade_research_conclusions` | 中文結論（decay / 資金週期 / 配置 / 執行建議 / filter） |

查詢範例：

```sql
SELECT conclusion_zh FROM copytrade_research_conclusions
WHERE batch_id = '00981a-copytrade-h20-20260617';

SELECT horizon, recycled_total_alpha_ntd, recycled_n_cycles
FROM copytrade_capital_cycle
WHERE batch_id = '00981a-copytrade-h20-20260617' AND entry_row = 'L1'
ORDER BY horizon;
```

---

## 7. 限制與實務風險

1. **樣本期**約一年；00981A 單一經理人 regime，外推有限。  
2. **未扣**手續費、證交稅、滑價、漲停買不到。  
3. **L0O/L0C** 僅供理論對照，不可執行。  
4. **多重 H 檢定**未校正。  
5. **α 對台指**，直接與 ETF 比較需另算（見 §4.4）。  
6. 持股公布若有延遲，實際可執行性接近 **L1**，而非 L0。

---

## 8. 延伸研究：跨 ETF 共識跟單（00981A + 00982A + 00990A）

### 8.1 研究假說

> 當 **同一交易日**、**同一檔股票** 出現在多檔主動 ETF（00981A／00982A／00990A）的 **新进/加码** 名單中（**跨 ETF 共識**），該訊號的選股品質高於「僅 00981A 單獨加码」→ 跟單報酬／α 應顯著更好。

這與現有 **Research OS** 的 `build_cross_etf_consensus`（`holdings_research.py`）、`consensus_trend.py` 方向一致，屬合理延伸。

### 8.2 現況資料盤點（2026-06-17 查詢）

| ETF | 持股快照數 | 快照區間 | 新进/加码 訊號 |
|-----|-----------|----------|----------------|
| **00981A** | 259 | 2025-05-27 ～ 2026-06-17 | 1,875 legs / 242 日 |
| **00982A** | **10** | **2026-06-04 ～ 2026-06-17** | 11 legs / **6 日** |
| **00990A** | **0** | — | **無資料** |

**跨 ETF 同日同股加碼（≥2 檔）**：全樣本僅 **2 筆**（皆 00981A + 00982A，無三檔共識）。

範例：

- 2026-06-08：2454（00981A + 00982A）
- 2026-06-10：2327（00981A + 00982A）

### 8.3 是否值得做？

**概念上：值得做。**  
**以目前資料：尚無法做有意義的統計檢定。**

| 維度 | 評估 |
|------|------|
| 假說合理性 | 高——多經理人同日押注同一標的，資訊含量應高於單一 ETF |
| 基礎建設 | 已有共識建構（`build_cross_etf_consensus`）、跟單回測（`copytrade_backtest`），**增量開發中等** |
| 樣本數 | **致命瓶頸**——2 筆共識無法做 Wilcoxon／t 檢定；00990A 完全缺資料 |
| 與 00981A-only 比較 | 需定義 **共識閾值**（≥2 ETF？≥3？）及 **對照組**（981A-only 訊號、或 981A 全訊號子集） |
| 預期產出 | 若 backfill 後每年僅數十筆共識，可能只能看 **效應方向**，難達 p<0.05 |

### 8.4 建議執行順序（若要做）

1. **Backfill 歷史持股**（與 00981A 同窗口）  
   - 00982A、00990A 目標：≥200 交易日快照。  
   - 確認 `sync_etf_holdings`／官網歷史檔是否可補。

2. **樣本 power 預估**（backfill 後立即做）  
   - 統計：共識訊號日數、與 981A-only 重疊比例。  
   - 若共識日 **< 30**，先報 descriptive，不承諾顯著性。

3. **回測設計**（與本文件方法對齊）  
   - **Treatment**：僅交易「當日 ≥K 檔 ETF 同時新进/加码」的 leg。  
   - **Control**：00981A 單獨新进/加码（同 L1H9、等權 1 萬、單池／全訊號兩種資金模型）。  
   - 檢定：配對或獨立樣本 Wilcoxon on 日均超額%。

4. **寫入 SQLite**  
   - 建議新表 `copytrade_consensus_compare` 或擴充 `copytrade_research_conclusions`（`analysis_type = consensus_filter`）。

5. **決策門檻**  
   - Backfill 後若年化共識可執行成交筆數 **< 10**：僅作監控特徵，不作獨立策略。  
   - 若 **≥ 20 輪** 且效應量大：可併入 evening / research digest 作為 **加碼條件**。

### 8.5 簡短結論

| 問題 | 答案 |
|------|------|
| 研究計畫值得做嗎？ | **值得規劃**，但 **必先補齊 00982A／00990A 歷史持股** |
| 現在做得出顯著結論嗎？ | **不行**（共識 2 筆、00990A 無資料） |
| 與本研究關係 | 在 L1H9 + 等權框架上，加一層 **訊號篩選**（共識 filter），其餘方法論可複用 |

---

## 10. 執行層假設檢驗登錄簿（Filter Hypothesis Registry）

> **目的**：讓統計／量化審閱者能追溯「假說 → 規則實作 → 樣本 → 檢定 → 採納／拒絕」全鏈路。  
> **窗口**：2025-05-28 ～ 2026-06-17 · **基準策略** L1H9（T+1 開盤買 · 持有 9 日 · 每日 10k 等權 · α vs IX0001 · 0 bps）。  
> **逐研究報告**：filter #2–#10 見 [`reports/research/_archive/00981a-filter-studies.md`](../reports/research/_archive/00981a-filter-studies.md) · **DB 結論**：`copytrade_research_conclusions`（`details_json` 含完整數字）。

### 10.1 共通評估契約（Filter Evaluation Contract）

| 項目 | 定義 |
|------|------|
| **主要終點（Primary）** | 相對基準的 **訊號日勝率勝率 Δ（pp）** 與 **累計 α**（`total_alpha_ntd`：每訊號日各 10k、持倉可重疊） |
| **次要終點（Secondary）** | **單池實現超額**（`recycled_total_alpha_ntd`：僅 1 萬輪動）；成分股層獲利勝率雙比例 z；配對 **α NTD 差** Wilcoxon |
| **對照** | 同一窗口、同一 L1H9 參數下的 **無篩選基準**（233 complete 訊號日 · 1872 legs） |
| **資金模型（Primary）** | 每個 complete 訊號日 **獨立 10k**，持倉可重疊（隱含約 H×1 萬部署） |
| **資金模型（Secondary）** | 單池 1 萬輪動；篩選後未成交 leg **不重新分配** |
| **顯著水準** | 探索性研究 · α=0.05 · **未**對多假設做 Bonferroni／FDR 校正 |
| **決策規則** | **僅當 Primary 改善（Δ勝率>0 且累計 α 升）且配對 p&lt;0.05** 才建議納入實盤；成分股層顯著但 Primary 不改善 → **拒絕全局採用** |
| **可重現** | 各研究 `batch_id` 寫入 `copytrade_*_filter_compare`；重跑見 §10.4 |

**審閱者應知限制**：樣本約 1 年、單一 ETF 經理人；未扣成本；部分 Wilcoxon 需 `scipy`（環境缺則為 None，比例檢定已用 math 後備）。

### 10.2 假設總表（#2–#10 + 執行層 + H1）

> **契約更新 2026-06-18**：Primary 改為 **累計 α**（`total_alpha_ntd`）；單池實現超額 為 Secondary。  
> **重跑**：20260618 批次 filter 可自 DB / 封存摘要查閱；H1 報告 [`hypothesis`](../reports/research/00981a-copytrade/20260618_00981a_hypothesis_l1h9.md)。

| # | 假設 | 狀態 | 判決 | Primary Δ勝率 | 累計 α / Leg 檢定 | 報告 |
|---|------|------|------|-----------------|-------------------|------|
| **H1** | 跳過單日 5–10 檔異動的訊號日 | ✅ 2026-06-18 | **採納** | **+3.32 pp**（66.84%） | 累計 **+39,983**（↑173）· 單池 +9,098（↓）· 5-10 vs 2-4 p=0.12 | [`hypothesis`](../reports/research/00981a-copytrade/20260618_00981a_hypothesis_l1h9.md) |
| **2** | 新进優先於加码（Initiation） | ✅ 2026-06-18 | **拒絕** | **−11.1 pp**（52.4%） | 累計 +7,135 vs +39,810 · p(W)=0.57 | [封存摘要](../reports/research/_archive/00981a-filter-studies.md) |
| **3** | 隔夜跳空區間（Gap Band） | ✅ 2026-06-18 | **拒絕** skip | skip_extreme **−2.6 pp** · mild **−9.2 pp** | 累計 −14,146 · leg 區間內/外 **p=0.020**（方向相反） | [封存摘要](../reports/research/_archive/00981a-filter-studies.md) |
| **4** | 大盤期貨隔夜風控（Macro Gap） | ✅ 2026-06-18 | **探索採納** `skip_tx<-3%`；拒絕 macro | skip_tx **+0.27 pp** · skip_macro **−1.7 pp** | 累計 +41,702（↑1,892）· 僅跳過 **2 日** · p(W)=0.0001 | [封存摘要](../reports/research/_archive/00981a-filter-studies.md) |
| **5** | v8 行為樹 Eligible | ✅ 2026-06-18 | **拒絕** | **−8.2 pp** | 累計 +9,591 vs +39,810 · leg p=0.16 | [封存摘要](../reports/research/_archive/00981a-filter-studies.md) |
| **6** | TA Pattern Gate | ✅ 2026-06-18 | **拒絕** 全局 | skip_overextended **−0.7 pp** · gate **−4.3 pp** | 累計 +2,984（↑）但勝率降 · leg **p=0.012** | [封存摘要](../reports/research/_archive/00981a-filter-studies.md) |
| **7** | 籌碼確認（外資+融資） | ✅ 2026-06-18 | **拒絕** | chip_confirm **−8.6 pp** | 累計 +7,221 vs +39,810 · leg p=0.48 | [封存摘要](../reports/research/_archive/00981a-filter-studies.md) |
| **8** | 開盤量價確認（09:05–15） | ✅ 2026-06-18 | **拒絕** 全局 | skip **−0.13 pp** | 累計 +36,375 vs +39,810 · leg **p=0.043** · L1+ **−8.3 pp** | [封存摘要](../reports/research/_archive/00981a-filter-studies.md) |
| **9** | 加碼力度分位（Conviction） | ✅ 2026-06-18 | **拒絕** | top30 **−1.2 pp** | 累計 +6,013 vs +39,810 · leg **p=0.061** | [封存摘要](../reports/research/_archive/00981a-filter-studies.md) |
| **10** | 多軌共振（Cross-Track） | ✅ 2026-06-18 | **拒絕** | triple **−5.6 pp** | 累計 +14,801 vs +39,810 · triple leg p=0.44 | [封存摘要](../reports/research/_archive/00981a-filter-studies.md) |
| **—** | 限價進場 −1/−2/−3%（執行） | ✅ 2026-06-18 | **拒絕** | −1% **−6.4 pp** · −3% **−7.1 pp** | 累計大降 · 成交 leg **p<0.0001** | [封存摘要](../reports/research/_archive/00981a-filter-studies.md) |

**小結（累計 α Primary）**：原 §10 九項 filter **仍全拒**（勝率多為負）；**新增採納** H1 `skip_5_10`；#4 `skip_tx<-3%` 為 **探索採納**（累計 α 升但僅 2 個跳過日，實盤意義有限）。#6 `skip_overextended` 累計 α 略升但勝率仍降 → 不採全局 skip。

### 10.3 逐假設：方法與判斷過程（成立／不成立）

#### #2 Initiation Filter

- **規則**：僅 `action=新进` vs 基準新进+加码。
- **樣本**：新进 50 legs · 42 訊號日（捕獲 18%）。
- **不成立依據**：勝率從 63.5% → 52.4%（−11.1 pp）；單池 α 少 +7,977；配對檢定 **不顯著**（p=0.81）→ 砍掉加码 **傷害** 績效，與「加码 資訊量低」直覺部分相反（加码 為多數樣本且貢獻 α）。
- **分開統計**：新进 子樣本 n 過小，**不得**單獨下「新进 更優」結論。

#### #3 Overnight Gap Band

- **規則**：gap = (T+1 open − T close) / T close；skip >+3% 或 <-2%；mild 0~+2%。
- **不成立依據**：skip_extreme 與 mild_band 皆 **降低** 勝率；極端跳空 leg 獲利勝率 **高於** 溫和區間（leg p=0.02，**與「追價風險」假說相反**）。
- **備註**：大幅高開 leg 在樣本內並非最差桶。

#### #4 Macro Gap Filter

- **規則**：T+1 `tech_risk` TX gap < −1.5% 或 TE 弱於 TX → skip／減半。
- **資料**：237/241 日來自 `tech_risk_overnight` backfill。
- **不成立依據**：**風險日勝率高於安全日**（65.96% vs 61.87%）；skip 宏觀風險日 **減少 α**。TX<-3% 僅 2 日 → 統計上無意義。
- **方法論轉折**：backfill 前用 spot 代理時結論可能相反；**以 backfill 後結果為準**。

#### #6 TA Pattern Gate

- **規則**：T 日 `entry_pattern`；跳過 OVEREXTENDED（無 STRONG_TREND 豁免）；可選僅 uptrend_pullback。
- **雙層結論**：成分股層跳過過熱 **顯著較差**（p=0.012）→ 過熱 leg 反而賺；訊號日層 skip **−0.7 pp**、配對 p=0.13 → **不採全局 gate**。
- **解讀**：經理人加碼過熱股在跟單框架下仍有 edge（動量延續），與「隔天被悶殺」假說不符。

#### #7 Chip Confirm

- **規則**：`foreign_net_5d>0` 且融資 5 日增幅 <5%。
- **不成立依據**：雙重通過 leg 勝率與未通過 **無差**（p=0.48）；篩選後勝率 **−8.6 pp**。
- **子檢定**：外資淨買 vs 非淨買 leg p=0.43。

#### #8 Opening Volume Confirmation

- **規則**：09:05–09:15 價≥昨收 且 量能≥0.8×前 5 日同時段；FinMind tick（5041/5083 日）。
- **雙層結論**：Leg 通過 **p=0.043**；訊號日 skip **Δ−0.13 pp**、配對 p≈0.10 → **監控用，不作主風控**。L1+ 09:15 進場 **−8.3 pp**。
- **子群**：跳空≥2% × 低量 **p=0.21**（假說方向對但不顯著）。

#### #9 Conviction Size（加碼力度前 30%）

- **規則**：`weight_delta`（缺則 `share_delta`）≥ 該股歷史加碼事件 70th 分位；個股史 <2 筆則用全局同 action 池。
- **樣本**：1875 legs · 通過 690（36.8%）。
- **雙層結論**：Leg 通過 63.52% vs 未通過 59.12%（**p=0.061**，10% 邊際、未達 5%）；訊號日 top30 **−1.22 pp**、實現超額 +6,013 vs +11,043 → **拒絕**。
- **仅加码·前30%**（新进 全留）：Δ **−1.48 pp**，無改善。

#### 執行層：限價 −1/−2/−3%

- **規則**：`limit=open×(1−k%)`；日 K `low` 觸價成交（Phase 1 粗估）。
- **不成立依據**：勝率全面下降；成交 leg 為 **弱勢選擇**（成交市價勝率顯著低於未成交，p<0.0001）→ 與 #8 延遲進場結論一致：**動量跟單不宜等便宜**。

#### #5 v8 Eligible Gate

- **規則**：T 日收盤 `is_00981a_v8_eligible`（`etf_add_consensus≥2` 或 v7 `RS×inv_weight_pct` 樹）；跟單僅保留 eligible leg。
- **樣本**：1875 legs · eligible **1224**（65.3%）· 樹 1219 · 共識 bypass **5**（樣本過小）。
- **雙層結論**：eligible leg 勝率 **低於** ineligible（59.59% vs 62.97%，p=0.16）；訊號日僅留 eligible **−8.23 pp**、實現超額 +2,991 vs +11,043 → **拒絕**。
- **解讀**：行為預測 v8 gate 優化的是「經理人會加哪檔」，**不等於**跟單 α 子集；被 gate 排除的 leg（高 RS 無共識等）在 L1H9 反而較賺。共識 bypass 僅 5 leg，無法單獨檢定。

#### #10 Cross-Track Confluence

- **規則**：T 日收盤三軌交集 — **vcp-tm** `valid_vcp` 且 composite≥min；**chunge** `layers_passed≥4`；**p6** `pm_bucket` 突破／觀察（DB 優先，否則歷史重算 money top20）。
- **樣本**：1875 legs · triple **96**（5.1%）· **79** 訊號日；單軌 VCP 898 · L4 746 · p6 270。
- **不成立依據**：triple 篩選勝率 **−5.63 pp**、實現超額 +7,462 vs +11,043；triple leg 勝率 **低於** 其他（57.0% vs 61.0%，p=0.44）→ **拒絕**。
- **成對子集**：VCP∩L4 **−0.88 pp**（最接近基準）；L4∩p6 **−4.43 pp**。
- **解讀**：共振 leg 為少數「技術＋籌碼」雙好，但 00981A 跟單 α 來自更廣義動量／加碼事件，收窄至三軌交集反而丟失 edge。

### 10.4 重跑指令（現行）

> §10 filter 研究（#2–#10、R-A/R-B、限價、開盤確認等）**已停損** — CLI 已自 repo 移除。歷史結論見 §10.2 與 [`00981a-retired-research.md`](00981a-retired-research.md)。

```bash
export PYTHONPATH=src
PY=.venv/bin/python
SCRIPT=scripts/run_00981a_copytrade_backtest.py
COMMON="--strategy-id L1H9 --write-db --write-report"

# 仍有效
$PY $SCRIPT --compare-hypothesis    $COMMON   # H1 skip_5_10 + H2
$PY $SCRIPT --matrix --max-hold 20 --write-db --write-report
$PY $SCRIPT --compare-allocation --strategy-id L1H9 --write-db
```

### 10.5 與其他文件的關係

| 文件 | 狀態 | 說明 |
|------|------|------|
| `reports/research/00981a-copytrade/00981a_hypothesis_scorecard.md` | ✅ §10 摘要已更新 | L1H9 filter 快覽；Track A–E 仍為 2026-06-15 快照 |
| `reports/research/00981a-copytrade/00981a_evidence_ledger.md` | ⚠️ 過時 | 同上 |
| `config/strategy.yaml` | SSOT | Strategy 層採納規格 · backtest；copytrade filter 契約見 **§10.1** |
| `config/research.yaml` | SSOT | 探索主題 · sweep · 採納前 |
| `copytrade_*_filter_compare` 表 | ✅ 機器可讀 | 各 `batch_id` 可 SQL 審計 |

### 10.6 距「統計專家完全認同」尚缺什麼

1. **事前登錄（Pre-registration）**：各假設 Primary 終點在跑回測前寫死（目前為事後歸納，§10.1 為追溯性契約）。  
2. **多重檢定校正**：8+ 探索假設建議報 q-value 或指定 **1 個** confirmatory 假設。  
3. **效應量 + CI**：除 pp 與 p 外，補 Wilson CI 或 bootstrap α 差信賴區間。  
4. **樣本外**：需第二時間窗或 walk-forward 驗證「拒絕」結論穩健。  
5. ~~**#10** 補齊後更新本表。~~（2026-06-17 已完成 #2–#10 + 限價執行研究）

### 10.7 已知誤區與復檢計畫

> **2026-06-17** · 回應「十項 filter 在其他情境很重要，為何跟單全滅？」之方法論澄清。  
> **結論先行**：§10 的「全部拒絕」≠ 因子在 00981A 無用；= 在 **§10.1 契約（skip 型 · 日度 Primary · L1H9）** 下，無一項通過採納門檻。

#### 10.7.1 三類問題錯置（最常見誤讀）

| 錯置 | 其他情境在問 | §10 跟單在問 | 後果 |
|------|--------------|--------------|------|
| **選股 vs 跟單** | 從 universe **該買誰**（VCP／p6／chunge） | 經理人**已加碼**後 **跟不跟哪幾檔** | 橫截面有效因子，在「已加碼」條件樣本裡辨識力下降或反向 |
| **行為預測 vs PnL** | v8：**會不會加** | 跟單：**加了會不會贏** | #5：ineligible leg 勝率 **高於** eligible（63.0% vs 59.6%） |
| **Setup vs 事件** | T 日技術 setup（突破前） | T 日**已公布**持股變化、T+1 進場 | #6/#10：過熱／VCP 規則與「加碼當下」時點錯位 |

條件機率寫法：

- 選股軌：`P(α \| factor)`  
- 跟單軌：`P(α \| factor, **PM 已加碼**)`  

兩者不必同號；Track E 亦顯示 **加码** 事件本身有 H+5 α，edge 主體在「跟加碼」，不在「再篩一層選股」。

#### 10.7.2 §10 契約造成的系統性偏誤（非實作 bug）

| 機制 | §10.1 設定 | 對 skip 型 filter 的影響 |
|------|------------|---------------------------|
| **Primary** | 訊號日 **勝率 Δ** + 實現超額；採納需配對 **p&lt;0.05** | 成分股層有訊號（#8 p=0.043、#9 p=0.061）但日層不改善 → 仍判 **拒絕** |
| **資金** | 被 skip leg **不重新分配** | 多檔異動日砍掉「因子判差」的一檔，常同步砍掉**當日最賺的一檔**；α 直接消失 |
| **動作** | 幾乎全測 **binary skip** | 因子在其他情境常作 **觀察／減碼／加權**，而非「不跟」 |
| **Horizon** | 固定 **L1H9** | VCP／漏斗多在 20–60d 校準；#8 延遲至 09:15 已 **−8.3 pp** |
| **探索數** | 9 假設 + 執行層、**未** Bonferroni | 「全拒絕」不能推論「全無預測力」，僅表示 **confirmatory 級別未過** |

#### 10.7.3 各假設：§10 結果 vs 可能設定問題

| # | §10 判決 | 被踢子集 vs 保留（成分股層） | 可能誤區／設定問題 |
|---|----------|---------------------------|-------------------|
| 2 | 拒絕 | 砍掉 **加码**（96% 樣本） | 假設方向可能錯：edge 在 repeat 加码，不在 rare 新进 |
| 3 | 拒絕 | 極端 gap leg **更賺**（p=0.02） | 「別追價」適用散戶自選；**跟 ETF 加碼** 像追動量，skip 方向可能反了 |
| 4 | 拒絕 | 風險日勝率 **高於** 安全日 | 宏觀 skip 假設與「主動調倉資訊」不相容 |
| 5 | 拒絕 | ineligible **勝率更高** | 把 **P(add)** gate 當 **P(α\|add)** gate |
| 6 | 拒絕 | 跳過 overextended **leg 更差**（p=0.012） | 跟單吃**動量延續**；TA gate 設成 skip 過熱 |
| 7 | 拒絕 | 通過 vs 未通過 **無差** | 籌碼規則偏「安靜吸筹」；加碼 leg 常已有熱度 |
| 8 | 拒絕（全局） | leg 通過 p=0.043；L1+ 延遲 **−8.3 pp** | 適合 **監控／日內**，不適 binary skip + 等開盤 |
| 9 | 拒絕 | leg 邊際 p=0.061；日層 **−1.2 pp** | top30% 略優但多檔異動日 skip 低 conviction 傷組合 α |
| 10 | 拒絕 | triple leg **低於** 其他（57% vs 61%） | 三軌交集 = 極窄「技術+籌碼」；跟單 α 在更廣義加碼 |
| — | 限價拒絕 | 成交 leg **顯著弱於** 未成交 | 「等便宜」= 挑到弱勢 leg；與 #8 一致 |

**統整**：多數不是「因子沒訊息」，而是 **(a) 問錯層級 (b) skip 方向與動量跟單相反 (c) 日度 Primary + 不 realloc 懲罰 skip**。

#### 10.7.4 復檢計畫（優先序 · 尚未實作）

以下 **不改寫 §10.2 既有判決**；通過後可新增 §10.8 或更新 scorecard「復檢欄」。

| 優先 | 代號 | 內容 | 契約調整 | 成功標準（草案） |
|------|------|------|----------|------------------|
| **P0** | **R-A** | **Leg 加權**（非 skip） | ✅ 2026-06-18 已跑 | 見 **§10.8** · 無採納 |
| **P0** | **R-B** | **反向 filter** | ✅ 2026-06-18 已跑 | 見 **§10.8** · 無採納 |
| **P1** | **R-C** | **Leg Rank IC**：因子分 vs leg α Spearman（#6/#7/#9/#10） | 新 Secondary；不判全局 skip | \|IC\| &gt; 0.03 且 bootstrap CI 不含 0 |
| **P1** | **R-D** | **分層樣本**：新进 vs 加码、單檔 vs 多檔異動日 分開估 | 避免 96% 加码 稀釋 | 至少一層 Primary 改善 |
| **P2** | **R-E** | **Horizon**：L1H20 / H+5 對齊 Track E | 策略矩陣列 | 與 L1H9 同方向結論則 §10 更穩 |
| **P2** | **R-F** | **#5 角色分離**：v8 作解釋變數（OLS leg α ~ eligible），不作 filter | 迴歸係數 + 分位 | 係數符號與「預測 add」文獻一致即可 |
| **P3** | **R-G** | **#10 p6 全歷史評分 backfill**（非 money top20 近似） | 減少實作差 | triple 結論方向不變則 #10 拒絕更可信 |

**建議執行順序**：R-A → R-B（低成本、直接驗證「動作／方向錯置」）→ R-C → 其餘。

**事前登錄（R-A/R-B）Primary**：仍用 **實現超額** 與 **勝率 Δ**；Secondary 報 leg IC。R-A/R-B 若通過，§10.2 原「拒絕」改標 **「skip 不採用；加權／反向可採」**，不刪原報告。

### 10.8 復檢結果（R-A / R-B · 2026-06-18）

> 報告：[封存摘要](../reports/research/_archive/00981a-filter-studies.md) · batch `00981a-r-a-l1h9-20260618` / `00981a-r-b-l1h9-20260618`

**採納門檻（§10.7.4）**：Δ勝率 &gt; 0 **且** **累計 α** 提升 **且** p(W) &lt; 0.05（單池實現超額 僅 Secondary 參考）。

#### R-A · Leg 加權（通過×1.5／未通過×0.5 · 全日 10k）

| 變體 | Δ勝率 | 累計 α | 單池 α | p(W) | 解讀 |
|------|---------|--------|--------|------|------|
| #9 conviction | −1.29 pp | +41,948（↑） | +11,534（↑） | 0.402 | 累計／單池皆升但勝率降 |
| #7 chip | −3.86 pp | +36,163（↓） | +12,402（↑） | 0.307 | **單池升、累計降** → 兩模型分歧 |
| #6 非過熱 | **+0.86 pp** | **+40,329（↑）** | +10,799（↓） | 0.117 | **新 Primary 下最接近採納**（p 未過） |
| H2 mom_core | −0.86 pp | **+43,228（↑）** | +11,861（↑） | **0.036** | 累計顯著升但勝率降 |

#### R-B · 動量／反向

| 變體 | Δ勝率 | 實現超額 | p(W) | 解讀 |
|------|---------|--------|------|------|
| gap↑&gt;+3% 加權 | −0.43 pp | +11,550（↑） | 0.573 | 略優於 skip 極端 gap |
| 過熱 加權 | −3.86 pp | +11,389（↑） | 0.074 | 加權仍傷勝率；α 邊際 |
| 動量∪ 加權 | −3.43 pp | +11,736（↑） | 0.196 | 同上 |
| 僅 gap&gt;+3% skip | −7.11 pp | +606 | 0.682 | **反向 skip 仍拒絕** |
| 僅過熱 skip | −17.37 pp | −1,436 | **0.024** | **最差**；過熱 leg 跟單勿 skip |

**復檢結論**：

1. **§10.2「skip 拒絕」維持**；無改標「可採 skip」。  
2. **部分因子改加權後實現超額 上升**（#9/#7/#3 加權）→ 支持 §10.7「動作錯置」假說，但 **未達** 三條件採納門檻。  
3. **#6 非過熱加權** 為 R-A 唯一勝率正 Δ（+0.86 pp）→ 後續 R-C 可對 `skip_overextended` 做 leg IC。  
4. **R-B 反向 skip（僅跟過熱／大 gap）明確劣於基準** → 動量跟單應 **全跟或加權**，不宜收窄子集。

```bash
PYTHONPATH=src .venv/bin/python scripts/run_00981a_copytrade_backtest.py --compare-recheck --strategy-id L1H9 --write-report
```

#### 10.7.5 審閱者應帶走的話

1. **其他軌道（p6／VCP／v8）與 §10 不矛盾**——服務對象不同（選股、brief、行為預測 vs 執行跟單）。  
2. **§10 已證偽的是**：「在 L1H9 等權跟單上，再加一層 **全局 binary skip** 能提升日度勝率」——此命題目前 **不成立**。  
3. **尚未證偽的是**：因子對 leg **排序／加權／監控** 是否有用——見 §10.7.4。

---

## 9. 重跑指令

```bash
# 完整 L×H 矩陣 + decay + 資金週期
PYTHONPATH=src .venv/bin/python scripts/run_00981a_copytrade_backtest.py \
  --matrix --max-hold 20 --write-db --write-report

# 僅資金週期（既有 batch）
PYTHONPATH=src .venv/bin/python scripts/run_00981a_copytrade_backtest.py \
  --analyze-capital-cycle --batch-id 00981a-copytrade-h20-20260617 --write-report

# 固定本金槽位 H 研究（例：9 万 9 槽）
PYTHONPATH=src .venv/bin/python scripts/run_00981a_copytrade_backtest.py \
  --analyze-fixed-slots --capital 90000 --slots 9 --write-report \
  --batch-id 00981a-copytrade-h20-20260617

# 等權 vs 按比例
PYTHONPATH=src .venv/bin/python scripts/run_00981a_copytrade_backtest.py \
  --compare-allocation --strategy-id L1H9 --write-db

# §11 L1-F1：leg 桶 × H 矩陣（5–10 桶延長 H 假說）
PYTHONPATH=src .venv/bin/python scripts/run_00981a_copytrade_backtest.py \
  --analyze-leg-bucket-horizon --write-report
```

---

## 11. L1 假設登錄簿（Hypothesis Registry）

> **目的**：把 L1 跟單的「為何有效、α 從哪來、哪些改動可採納」收斂成可檢定假說鏈。  
> **窗口**：2025-05-28 ～ 2026-06-17 · **基準** L1 T+1 開盤 · 等權 1 萬/訊號日 · α vs IX0001 · 0 bps。  
> **與 §10 關係**：§10 = filter/skip 是否改善 **全局** L1H9；§11 = **L1 自身結構**與 **條件式持有** 假說。

### 11.1 L1 定義（再述）

| 項目 | 規格 |
|------|------|
| 訊號日 T | ETF 持股快照公布日（`compute_etf_holdings_changes` 新进/加码） |
| 執行 | **T+1 開盤**買當日全部觸發 leg（**L1**） |
| 出場 | 進場後第 **H** 交易日收盤賣 |
| Primary | 每訊號日 10k、持倉可重疊 → `total_alpha_ntd`、勝率 |
| Secondary | 單池 1 萬輪動 → `recycled_total_alpha_ntd` |

### 11.2 分層解剖（實證摘要）

| 層 | 變數 | L1H9 要點 | L1H20 要點 |
|----|------|-----------|------------|
| **時間 H** | 持有天數 | H5 起顯著；Optimal hold (H*) **H9**（單池） | 累計 α 峰值；捕獲率 ~5% |
| **leg 桶** | 1 / 2–4 / 5–10 / 11+ | α **50% 來自 2–4**；**5–10 ≈0** | 5–10 **轉正** +20k |
| **行為** | 新进 vs 加码 | **加码 主導**（仅新进 −32k α） | 同左 |
| **regime** | broadening 等 | 主樣本 n=193；5–10 在 broadening 仍負 | 探索 |
| **leg 微結構** | gap×p5d | 深 gap leg 顯著更賺（p=0.005） | 加權候選 R-A |
| **資金模型** | 重疊 vs 輪動 | 9 萬重疊 cum α vs 1 萬 H9 輪動 | 勿混談 |

**統一機制假說**：L1 α 來自 **完整再平衡籃子**；縮小 basket（v8/v9/filter）常砍掉仍為正的 leg → 組合 α 下降。

### 11.3 假說登錄簿

#### 區塊 A · 基礎有效性

| ID | 假說 | 終點 | 現況 |
|----|------|------|------|
| **L1-A1** | T+1 跟實際 leg 贏台指 | mean excess>0 · H≥5 | **支持**（p<0.05） |
| **L1-A2** | α 含選股非純 beta | CAPM α | **支持** |
| **L1-A3** | L1 優於買 ETF | 同進出 gross | **支持**（H9 單池 +7k，未扣成本） |
| **L1-A4** | 隔夜資訊成本 | L0O α > L1 α | **支持**（oracle 上限） |

#### 區塊 B · 結構分層

| ID | 假說 | 現況 |
|----|------|------|
| **L1-B1** | H9 α 由 2–4 leg 日驅動（>40% cum α） | **支持** |
| **L1-B2** | H9 的 5–10 leg 日為 α 黑洞 | **支持**（cum ≈0，p≈1） |
| **L1-B3** | H20 修復 5–10 桶 | **支持**（見 §11.4 L1-F1） |
| **L1-B4** | 11+ bulk 為 H9 第二引擎 | **支持** |
| **L1-B5** | repeat 加码 承載 α（非新进） | **支持**（#2 initiation 拒絕） |

#### 區塊 C · 可執行改進

| ID | 假說 | 現況 |
|----|------|------|
| **L1-C1** | skip 5–10 **日** @ H9 提勝率 | **探索採納**（+3.3 pp；單池 α ↓） |
| **L1-C2** | v8/v9 gate 提升 L1 α | **拒絕** |
| **L1-C3** | 深 gap leg 加權 | **開放**（R-A） |
| **L1-C4** | 過熱 leg skip | **拒絕** |
| **L1-C5** | weight_pct 優於等權 | **拒絕** |

#### 區塊 D · Regime

| ID | 假說 | 現況 |
|----|------|------|
| **L1-D1** | 弱市縮短 H | **拒絕**（contraction 仍長 H cum α 高，n 小） |
| **L1-D2** | restrictive 日 skip | **拒絕** |
| **L1-D3** | broadening 內 5–10 @ H9 仍差 | **支持** |

#### 區塊 E · 機制

| ID | 命題 | 現況 |
|----|------|------|
| **L1-E1** | 實際 basket α > 任意 top_k | **支持** |
| **L1-E2** | miss leg 仍有正 excess | **支持** |
| **L1-E5** | 行為 KPI ⊥ 跟單 α | **支持**（OOS §5） |

### 11.4 探索實驗 · L1-F1（5–10 桶 × H 矩陣）

**問題**：5–10 leg 日在 H9 近乎零 α — 應 **skip**（L1-C1）還是 **延長 H**？

| 項目 | 定義 |
|------|------|
| **H0** | 5–10 桶延長至 H20 不能同時提升累計 α 與勝率（相對 H9） |
| **Ha** | 延長 H 優於 skip（該桶 cum α 與勝率均升） |
| **資料** | `00981a-copytrade-h20-20260617`（H≤20）+ `00981a-copytrade-l1h45-20260618`（H27） |
| **程式** | `src/copytrade_leg_bucket_horizon.py` · `--analyze-leg-bucket-horizon` |
| **報告** | `reports/research/00981a-copytrade/*_00981a_l1f1_leg_bucket_horizon.md` |
| **DB** | `copytrade_regime_horizon`（`bucket_field=leg_count`）· `copytrade_research_conclusions`（`leg_bucket_horizon`） |

**採納門檻**：5–10 桶配對日 · cum α(H20) > cum α(H9) **且** 勝率升 **且** 配對 Wilcoxon p<0.05。

**結果（batch `00981a-l1f1-leg-bucket-20260618` · 2026-06-18）**：

| 指標 | H9（5–10 桶） | H20（5–10 桶） | Δ |
|------|---------------|----------------|---|
| 配對 n | 44 | — | — |
| 累計 α | +1,571 | +20,070 | **+18,500** |
| 勝率% | 52.3% | 68.2% | **+15.9 pp** |
| 配對 p(W) | — | — | **0.0005** |

**判決**：**`adopt_extend_h`（採納）** — 5–10 leg 日延長至 H20 同時提升 α 與勝率；優於 H9 skip（L1-C1 僅提勝率、犧牲 cum α）。

**各桶 H*（累計 α 峰值）**：`2-4` / `5-10` / `11+` / `1` 在樣本內皆指向 **H27**（需 extended batch）；全局單池 Optimal hold (H*) 仍 **H9**（§5.2）。

報告：[`l1f1_leg_bucket_horizon`](../reports/research/00981a-copytrade/20260618_00981a_l1f1_leg_bucket_horizon.md)

**實務含義**：

- **L1-F1 已採納**：5–10 leg 日應 **延長 H 至 20**（需額外槽位／資金），而非 skip。
- **與 L1-C1 並存策略**：全局 H9 全接；僅當資金允許時，對 5–10 日單獨 **H20 槽** 持有。
- H9 全局仍可用 skip_5_10 **提勝率**（+3.3 pp），但 cum α 幾乎不變 — 與延長 H 解決不同問題。

### 11.5 探索實驗 · L1-H3（桶 × H 交互檢定）

**問題**：延長 H（H20−H9）的邊際效益是否因 leg 桶而異？還是全局長持效應？

| 項目 | 定義 |
|------|------|
| **H0** | 各桶同日 Δα(H20−H9) 來自同一分布 |
| **Ha** | 桶 × H 存在交互；5–10 日均 Δα 可能高於 2–4 |
| **檢定** | 各桶 Wilcoxon(Δα vs 0) · Kruskal–Wallis 跨桶 · Mann–Whitney 5–10 vs 2–4 |
| **程式** | `src/copytrade_leg_bucket_horizon.py` · `--analyze-l1-h3` |
| **報告** | `reports/research/00981a-copytrade/*_00981a_l1h3_interaction.md` |
| **DB** | `copytrade_research_conclusions`（`leg_bucket_h_interaction`） |

**結果（batch `00981a-l1h3-20260618` · 2026-06-18）**：

| 檢定 | p | 結論 |
|------|---|------|
| Kruskal–Wallis（各桶 Δα） | 0.310 | **不拒絕 H0**（無全局交互） |
| 5–10 桶 Wilcoxon(Δα vs 0) | 0.0005 | **顯著**（延長 H 對該桶有效） |
| 5–10 vs 2–4 Mann–Whitney（日均 Δα） | 0.115（單尾） | **未達顯著** |

| 桶 | n | cum Δα | **日均 Δα** | H9 mean excess% |
|----|---|--------|-------------|-----------------|
| 2–4 | 93 | +24,843 | 267 | +2.01% |
| **5–10** | 44 | +18,500 | **420** | **−0.04%** |
| 11+ | 57 | +13,608 | 239 | — |

**判決**：**`5_10_marginal_only`** — 5–10 桶 H9 近乎零 excess，延長 H 顯著修復；但跨桶分布差異未達 KW 顯著（可能為 **全局長持效應**，見 L1-H1）。5–10 **日均** Δα（420）高於 2–4（267），方向符合子假說，但 Mann–Whitney 單尾 p≈0.115 未過門檻。

報告：[`l1h3_interaction`](../reports/research/00981a-copytrade/20260618_00981a_l1h3_interaction.md)

**實務含義**：

- L1-F1 採納（5–10 → H20）仍成立；L1-H3 補充：**緊迫性**來自 H9 近零，非獨特交互。
- 下一步 **Step 2（L1-P1～P3）**：在單池資金下比較 uniform H9 / 5–10 extend / 分桶最優 H。→ **見 §11.6**

### 11.6 探索實驗 · L1-P1～P3（分桶持有政策模擬）

**問題**：L1-F1 採納的「5–10→H20」在 **單池資金約束** 下是否仍優於全局 H9？

| 項目 | 定義 |
|------|------|
| **P1** | 全局 H9（基準） |
| **P2** | 5–10 leg 日 → H20，其餘 H9 |
| **P3_sweet** | 各桶矩陣累計 α 峰值 H（樣本內皆 H27） |
| **P3_practical** | 5–10→H20，11+→H15，其餘 H9 |
| **P4** | skip 5–10 日 @ H9（L1-C1 對照） |
| **Secondary** | 單池輪動：上一筆 exit 前不接新訊號 |
| **程式** | `--analyze-l1-policy` |
| **報告** | `reports/research/00981a-copytrade/*_00981a_l1policy.md` |

**結果（batch `00981a-l1policy-20260618` · 單池 1 槽）**：

| 政策 | 累計 α | 單池實現超額 | 捕獲% | Δ實現超額 vs P1 |
|------|--------|------------|-------|-------------|
| **P1** H9 | +39,810 | **+11,043** | 11.6% | — |
| **P2** 5–10→H20 | +60,053 | +4,446 | 8.7% | **−6,597** |
| P3_sweet H27 | +158,137 | +11,047 | 4.1% | +4 |
| P3_practical | +70,242 | +6,431 | 8.7% | −4,612 |
| P4 skip 5–10 | +39,983 | +9,098 | 14.4% | −1,945 |

**判決**：**`explore_bucket_sweet`**（單池 1 槽最佳名義為 P3_sweet，實質與 P1 持平）；**P2 在單池 1 槽下劣於 P1**。

**9 槽對照（90k NTD · 全捕獲基準 P1 = +39,810）**：

| 政策 | 9 槽實現超額 | Δ vs P1 | 捕獲% |
|------|------------|---------|-------|
| P1 H9 | +39,810 | — | 100% |
| **P2** 5–10→H20 | **+55,158** | **+15,348** | 82% |
| P3_practical | +53,348 | +13,538 | 72% |
| P3_sweet H27 | +69,828 | +30,018 | 37% |

**實務含義**：

- L1-F1 的 Primary 採納（累計 α）在 **單池 1 槽** 下被捕獲率下降抵消（P2 實現超額 +4,446 < P1 +11,043）。
- **有足夠槽位時 P2 仍值得做**：9 槽下 P2 實現超額 **+38%**（+15,348 vs P1）。
- 落地方式：**5–10 日獨立 H20 槽**，與全局 H9 槽並行，而非單池混用。
- P4 skip 提捕獲率（14.4%）但 1 槽實現超額 仍低於 P1。

報告：[`l1policy`](../reports/research/00981a-copytrade/20260618_00981a_l1policy.md)

### 11.7 待驗證（下一輪）

| ID | 假說 | 備註 |
|----|------|------|
| **L1-F6** | 成本敏感度 +15 bps | L1-A1 / P2 穩健性 |
| **L1-R1** | OOS 政策模擬 | P2 單池劣勢是否穩定 |
| **L1-F2** | 深 gap leg ×1.5 加權 | R-A 契約 |
| **L1-F3** | Leg Rank IC → 加權 | R-C |
| **L1-F4** | 982A 重疊日監控 | 日曆標記 |
| **L1-F5** | first cohort 獨立策略 | Track B |

---

_文件版本：2026-06-18 · copytrade-v6（§11 L1-F1 · L1-H3 · L1-P1-P3）_
