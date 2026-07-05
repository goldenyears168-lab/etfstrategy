# RRG Improving lifecycle research（Improving 生命週期研究）

> **Layer**: research（探索性 · 未採納至 `config/strategy.yaml`）  
> **Status**: active · 2026-06-30  
> **Universe**: ETF 成分股監測名單（`load_etf_constituent_watchlist` · 149 檔 · `DEFAULT_ETF_CODES`）  
> **Benchmark**: 台指加權 `IX0001`（本地 `daily_bars`；缺日時 FinMind `TAIEX` 補洞）  
> **Code**: `src/research/backtest/rrg_improving_lifecycle_backtest.py`  
> **Runner**: `scripts/run_rrg_improving_lifecycle_backtest.py`  
> **Artifacts**: `reports/research/rrg/improving_lifecycle_backtest_YYYYMMDD.{md,json}`

---

## 1. 研究動機

採納策略 **RRG mono fresh**（`buy-signal-radar` · `rrg-fresh-mono`）以 **昨收 PIT mono tier2 新鮮池** 為 SSOT，對 **盤中才翻進 Improving 並大漲** 的標的覆蓋不足（例：2645 長榮航太 2026-06-30 盤中 provisional fresh）。

本研究沿 **Julius de Kempenaer RRG 四象限**，在 **Improving（動能修復帶）** 內建立 **分階段生命週期（lifecycle）**，回答：

1. 各階段 **隔日（D+1 close）** 期望值、波動、尾部機率（P(≥5%)）  
2. **Setup core 蓄力帶** 是否提高隔日 burst 機率  
3. **6/30 實例**（2345 智邦、3661 世芯等）在統計分布中的位置  
4. 如何對 **全監測名單** 計算 **7/1 優先序 · 條件期望值**

**Non-goals**：非下單規格 · 非 live gate · 不取代 `rrg-mono-hold7` 採納策略。

---

## 2. 術語與 RRG 基礎

| 符號 | 定義 |
|------|------|
| **RS-Ratio（RV）** | 個股相對大盤強度 · baseline = 100 · >100 偏 Leading 側 |
| **RS-Momentum（MV）** | RS-Ratio 的動能 · baseline = 100 |
| **Improving 象限** | RV ≤ 100 且 MV > 100 · 相對仍弱但 **動能轉正** |
| **Leading 象限** | RV > 100 且 MV > 100 |
| **Lagging 象限** | RV ≤ 100 且 MV ≤ 100 |
| **PIT** | 訊號日 T 僅用 `date ≤ T` 收盤資料 |
| **4 日軌跡** | `LOOKBACK=4` · `_feat()` 產出 `quadrants[]` · `trend` · `disp` · `mono_up` |
| **disp** | 4 日 (ΔRS-Ratio, ΔRS-Momentum) 歐幾里得位移 |
| **mono_tier2** | `up_right` + 終點 **Leading** + `disp∈[1,2)` + `mono_up=True` |
| **burst** | 當日或隔日收盤漲幅 **≥ 5%**（可 `--burst` 調整） |

RRG 計算：`src/rrg_rotation.py` · `compute_rrg_panel()` · WMA length=20。  
特徵閘門：`src/rrg_mono_daily_brief.py` · `_feat()` · `_mono_tier2()`。

---

## 3. 生命週期 Stage 定義（信號日 D 收盤判定）

所有 Stage 在 **信號日 D 收盤** 計算，評估 **D+1 收盤報酬**（除非註明為當日）。

### S0 · fresh flip → Improving

- **條件**：D 日象限 = `improving` · D−1 日象限 **≠** `improving`  
- **語意**：剛從 Lagging / Weakening / Leading 翻進 Improving  
- **歷史**：約 **33%** 的 Improving +5% 發生在 **Improving 第 1 天**（事件股路徑）

### S1 · Improving 第 1–3 天

- **條件**：D 日象限 = `improving` · 連續 Improving streak ∈ [1, 3]  
- **語意**：修復 **早段**（含 S0 之後的第 2、3 天）

### S2 · Setup core 蓄力帶

在 **base setup** 之上，加上 **價格過濾** 與 **相對大盤過濾**：

**base setup**（必要）：

| 條件 | 說明 |
|------|------|
| `trend == up_right` | 4 日軌跡綜合往右上 |
| 4 日 `quadrants` **皆** `improving` | 已在 Improving 軌道 ≥4 日 |
| **非** `mono_tier2` | 非 mono 加速 Leading 規格 |
| `mono_up == False` | 3 段軌跡長度非遞增 |
| `disp ∈ [0.25, 0.85)` | 位移適中 · 非過度延伸 |

**Setup core 加分 / 排除**（研究用 · 非 base 必要）：

| 條件 | 說明 |
|------|------|
| **加分 B** | `excess_vs_IX0001 ∈ [-2%, -0.3%]`（信號日相對 **前一日大盤** 跑輸但未崩） |
| **排除** | 信號日漲幅 `≥ +1%`（避免已 chase） |
| **排除** | 信號日跌幅 `≤ -2%`（深跌延續風險） |

> **excess 定義**：`sig_ret − bench_prev_ret` · `bench_prev_ret` = 前一 **交易日** 收盤至再前一交易日之大盤報酬。  
> **長假注意**：D−1 若跳日（如 6/26→6/29），excess 基準為 **長區間大盤報酬**，解讀需謹慎（見 §8）。

### S2a · Setup core + RV 98–99

- **條件**：S2 Setup core + `RS-Ratio ∈ [98, 99)`  
- **語意**：Improving 尾段 · **即將進 Leading**（快貼 100 門檻）

### S3 · Improving 大漲日（burst）

- **條件**：D 日象限 = `improving` · 當日漲幅 **≥ 5%**  
- **語意**：**動能釋放日** · 非蓄力日

### S4 · RV 98–99（overlay）

- **條件**：`RS-Ratio ∈ [98, 99)`（可與各 Stage 交叉）  
- 單獨統計時樣本大 · 作為 **位置標籤** 優於硬篩 **RV 95–98**（後者長期統計更差）

---

## 4. 生命週期地圖（概念）

```text
[Lagging / 弱]
    ↓
S0  翻進 Improving（~33% 當日即 ≥5% · 事件股）
    ↓
S1  Improving 第 1–3 天（修復早段 · 高頻 · 隔日期望 ≈ 0）
    ↓
base setup（4 日皆 improving · up_right · disp 適中 · 非 mono）
    ↓
S2  Setup core（+ 昨跑輸大盤 · 當日未 chase · 未深跌）→ 隔日 mean ≈ 0 · P(burst) ~1%
    ↓
S2a（+ RV 98–99）→ 略優 S2 · 仍低頻
    ↓
S3  Improving 大漲 ≥5%（釋放日）→ 隔日 mean ~+0.3% · 強依賴台指方向
    ↓
[Leading / RV > 100]
```

**兩個母體勿混用**：

| 母體 | 信號日價格 | 隔日角色 |
|------|-----------|---------|
| **Setup core（S2）** | 橫盤 / 小回 · 刻意未釋放 | 低期望 · 略提高 ** rare burst** |
| **S3 大漲** | 已 +5%+ | 略正延續 · **綁大盤 beta** |

---

## 5. 回測方法論

### 5.1 資料與區間

| 項目 | 值 |
|------|-----|
| 樣本區間 | 2015-01-09 → 2026-06-25（信號日） |
| 成分股 | 149（ETF constituent watchlist） |
| 訊號日數 | ~2,790 |
| burst 門檻 | 5%（預設） |

### 5.2 輸出指標（每 Stage）

| 指標 | 定義 |
|------|------|
| `n_events` | 符合條件之 (date, stock_id) 列數 |
| `mean_per_day` | `n_events / 訊號日數` |
| `next_mean_pct` | D+1 收盤報酬平均 |
| `next_std_pct` | D+1 報酬標準差 |
| `next_win_rate` | P(D+1 > 0) |
| `p_next_ge_burst` | P(D+1 ≥ 5%) |
| `ew_portfolio_mean` | 同日多檔 **等權** 後之跨日平均 |

### 5.3 Transition（路徑機率）

| ID | 路徑 |
|----|------|
| `S0_same_day` | S0 翻進當日 ≥ burst |
| `S2_to_burst_D1` | Setup core → **隔日** ≥ burst |
| `S2_to_burst_2d` | Setup core → **兩日內** 任一日 ≥ burst |

**Lift 基準（③）**：  
`P(Improving ≥5% 任意成分股交易日)` ≈ **0.52%**（= Improving burst 事件 / 全部 stock-days）。  
有別於「Improving 當日條件機率」~2.74% · 勿混用。

### 5.4 分層（Stratified）

- S3 × 隔日台指上/下  
- S3 × improving streak（1 / 2–3 / ≥6）  
- Setup core × `disp` 收斂（Δdisp < −0.1）vs 擴張  

---

## 6. 回測結果摘要（全樣本 · 2015–2026）

### 6.1 Stage 表（信號日 → 隔日）

| Stage | 標籤 | N | 日均檔數 | 隔日均值 | 隔日 σ | 勝率 | P(隔日≥5%) |
|-------|------|---|---------|---------|--------|------|-----------|
| **S0** | fresh flip | 8,552 | 3.06 | +0.06% | 2.36% | 45.5% | 3.08% |
| **S1** | Improving 1–3 天 | 22,494 | 8.06 | +0.07% | 2.31% | 46.2% | 2.90% |
| **S2** | Setup core | 3,229 | 1.16 | +0.02% | 1.62% | 44.3% | **0.99%** |
| **S2a** | Setup + RV 98–99 | 941 | 0.34 | +0.05% | 1.73% | 47.3% | 0.96% |
| **S3** | Improving ≥5% | 1,784 | 0.64 | **+0.33%** | 3.79% | 45.6% | **10.71%** |
| **S3a** | S3 + RV 98–99 | 458 | 0.16 | +0.33% | 3.59% | 45.6% | 9.83% |

**解讀**：

- **S0/S1**：隔日 **mean ≈ 0** · 靠 **P(≥5%) 尾部**（~3%）  
- **S2**：刻意壓縮波動（σ 1.6%）· mean ≈ 0 · **P(≥5%) ~1%**  
- **S3**：唯一 **mean 明顯為正** · 但勝率仍 ~46% · **非高勝率策略**

### 6.2 Transition

| 路徑 | 命中率 | 基準③ | Lift×③ |
|------|--------|-------|---------|
| S0 當日 ≥5% | **6.86%** | 0.52% | **13.2×** |
| S2 → 隔日 ≥5% | **0.99%** | 0.52% | **1.9×** |
| S2 → 兩日內 ≥5% | 0.99% | 0.52% | 1.9× |

Setup core **略放大 rare burst（~1.9×）** · 絕對機率仍 **~1%/日**。

### 6.3 Improving streak × 當日 burst

| 連續 Improving 天數 | P(當日≥5%) | burst 後隔日均值 |
|--------------------|------------|-----------------|
| 1 | **6.86%** | +0.32% |
| 2 | 2.56% | +0.02% |
| 3 | 2.37% | +0.64% |

### 6.4 S3 隔日 × 台指（最強規律）

| 隔日台指 | 個股隔日均值 | 勝率 |
|---------|-------------|------|
| **上漲** | **+1.24%** | 55% |
| **下跌** | **-0.84%** | 33% |

S3 隔日 **+0.33% 平均幾乎全由大盤方向解釋**。

### 6.5 其他分層

| 子群 | 隔日均值 |
|------|---------|
| Setup core + disp 收斂 (Δ<-0.1) | +0.06% |
| Setup core + disp 擴張 (Δ>0.1) | **−0.04%** |
| S3 · improving ≥6 天後 burst | +0.45% |
| RV 95–98（Setup 上加篩） | 長期 **差於** RV 98–99 · 勿用 |

### 6.6 近 252 交易日

| Stage | n | mean/day | 隔日 mean | P(≥5%) |
|-------|---|----------|-----------|--------|
| S0 | 902 | 3.58 | +0.33% | 8.09% |
| S1 | 2,378 | 9.44 | +0.30% | 6.90% |
| S2 | 271 | 1.07 | +0.01% | 2.58% |
| S2a | 93 | 0.37 | +0.15% | 2.15% |

---

## 7. 各 Stage 統計原理（為何數字長這樣）

### S0 / S1 · mean ≈ 0 但有尾部

- 翻進 Improving **當日** P(≥5%) ≈ **6.9%**（~2.5× Improving 日均）  
- 隔日把「當日已噴 / 未噴」混合 → **平均被拉平**  
- 適合當 **事件觀測** · 非隔日穩賺規則

### S2 · 蓄力 = 低波 + 低頻 burst

- 排除大漲大跌日 → **σ 壓到 ~1.6%**  
- 隔日 mean **≈ 0 是設計結果**（壓縮態）  
- 價值在 **Lift ~1.9×** 的尾部 · 非 mean alpha

### S3 · 釋放後略正 + 高 σ + 綁 beta

- 當日已釋放動能 → 隔日 **延續 vs 回吐** 並存  
- **台指順風** 時平均 **+1.2%** · 逆風 **−0.8%**

---

## 8. 實證案例 · 2026-06-29 / 06-30

> 本地 `stocks.db` 日線至 2026-06-26 · 案例以 FinMind 補 6/29–6/30。

### 8.1 2026-06-30 市場

- 台指 **+2.50%** · 順風 burst 日

### 8.2 Stage 命中（6/30 收盤）

| Stage | 檔數 | 備註 |
|-------|------|------|
| S0 | 5 | 含 6278 台表科 |
| S1 | 10 | — |
| **S2 / S2a** | **0** | 無蓄力帶 |
| **S3** | **7** | 見下表 |

### 8.3 S3 名單（6/30）

| 代號 | 漲跌 | Improving 天數 | RV | 軌跡備註 |
|------|------|---------------|-----|---------|
| 1815 富喬 | +9.90% | 7 | 99.1 | 4 日皆 improving |
| 6278 台表科 | +9.79% | **1** | 96.5 | **S0+S3** · lag 起點 |
| 3037 欣興 | +9.63% | 2 | 99.8 | lag 起點 |
| 3443 創意 | +9.62% | 9 | 99.5 | 4 日皆 improving |
| 2467 志聖 | +8.96% | 4 | 97.9 | 4 日皆 improving |
| 2351 順德 | +7.59% | 3 | 99.9 | lag 起點 |
| 2345 智邦 | +6.62% | 7 | 97.8 | 4 日皆 improving |

**共同特徵**：

- 皆 **Improving 內 burst** · RV 多 **96–99.9**（快貼 Leading）  
- **兩劇本**：S0 當日噴（6278）vs 老 Improving 加速（1815、3443、2345）  
- **6/29 昨超額不一致** · 非「全部先弱再強」（2345 6/29 跑輸 · 1815/6278 6/29 已強）

### 8.4 6/29 Setup 與長假 excess

6/29 前一根 K 為 **6/26**（台指 6/26→6/29 **−3.64%**）。

| 代號 | 6/29 漲跌 | excess* | base setup | S2 core |
|------|----------|---------|------------|---------|
| 2345 | −1.68% | **+1.96%** | ✅ | ❌ |
| 3661 | +2.97% | +6.61% | ✅ | ❌ |
| 1216 | +1.47% | +5.11% | ✅ | ❌ |

\*excess = 6/29 漲跌 − 台指(6/26→6/29)

**6/29 有 base setup · 無 S2 core** → 2345 路徑為 **6/29 base → 6/30 S3** · 非「6/29 已是 S2 core」。

### 8.5 3661 世芯（6/30 +4.89%）

- **準 S3**（<5% 門檻）· base setup 仍成立  
- 人工可歸 **釋放帶** · 統計模型若僅 S1(1–3) 會 **低估**

---

## 8.6 S2 → 隔日 S3 burst · 32 筆案例研究（2026-06-30）

**Runner**: `scripts/run_rrg_s2_burst_case_study.py`  
**Artifacts**: `reports/research/rrg/s2_to_burst_case_study_YYYYMMDD.{md,json}`

### 結論摘要

| 發現 | 數字 |
|------|------|
| S2 → 隔日 ≥5% | **32 / 3231 = 0.99%** |
| 隔日 alpha（個股−台指）| hit 均值 **+6.81%** · **31/32** alpha≥4% |
| 台指跌日仍 burst | **10/32** alpha≥5% |
| Rule B（sig flat + disp↓ + RV<99 + streak≥6）| **4.76%** · p≈**8.6e-05** · 僅 capture **6/32** |

**三原型**：A 蓄力簧（25%）· B 貼 Leading RV≥99.5（25%）· C 預追價 sig 0.5–1%（16%）· 其餘 idiosyncratic。

**外力**：題材/籌碼/個股事件（2344×3 · 3661/3443 等）· 非 RRG 可完全編碼 · 法人淨買 lift 弱（44% vs 33%）。

---

用於 **信號日 D 收盤 → 預測 D+1** · 2026-06-30 實作排序 7/1。

### 9.1 步驟

1. 對監測宇宙每檔計算 D 日 Stage 與特徵（streak · RV · sig_ret · excess · disp_d）  
2. 查表 **Stage 條件 E[nxt]**（§6 回測）  
3. 若該股 **S3 後隔日歷史 ≥3 次** · 混合：`E = 0.55 × E_stage + 0.45 × E_sid_hist`  
4. **台指情境**：S3 池 · 中性 → 上調 **+0.91%** / 下調 **−1.17%**（= 回測 mkt_up/down − 中性）

### 9.2 Stage 先驗 E[nxt]（中性台指）

| 狀態 | E[nxt] |
|------|--------|
| S3 · streak = 1 | +0.32% |
| S3 · streak 2–3 | +0.30% |
| S3 · streak ≥ 4 | +0.35% |
| S3 · hot_ctx* | +1.57% |
| S2 / S2a | +0.02% ~ +0.05% |
| S0 | +0.06% |
| S1 | +0.07% |
| near_burst（improving · base · 3%≤sig<5%） | +0.25% |
| 其他 improving | +0.04% |
| 其餘 | 0% |

\*hot_ctx：前日 excess≥5% 且 信號日台指≥2% · **n=17** · 過擬合風險高

### 9.3 2026-06-30 → 7/1 排序（摘要）

| 優先 | 代號 | E[7/1] 中性 | 備註 |
|------|------|------------|------|
| 1 | 6278 | +2.87% | S0+S3 · n=3 · **高變異** |
| 2 | 3443 | +0.56% | S3 · 個股史穩 |
| 3 | 2345 | +0.33% | S3 · AI 鏈 |
| 4 | 3037 | +0.18% | S3 |
| 5 | 2351 | +0.13% | S3 |
| 6 | 1815 | +0.07% | S3 |
| ↓ | 2467 | **−0.49%** | S3 · 個股 S3 後史差 · **剔除** |

**149 檔中僅 S3 七檔 E 明顯 > 0** · 其餘 **≈ 0%**。

### 9.4 近一個月 walk-forward 規則 sweep（2026-06-30）

**Runner**: `scripts/run_rrg_improving_lifecycle_monthly_sweep.py`  
**Artifacts**: `reports/research/rrg/improving_lifecycle_monthly_sweep_YYYYMMDD.{md,json}`

| 窗口 | 區間 | 用途 |
|------|------|------|
| Train | 2026-03-20 → 2026-05-25（44 日） | 規則候選排序 |
| Test OOS | 2026-05-26 → 2026-06-25（22 日） | **held-out 近一個月** |
| Rolling | 8×22 日 fold 回溯 | 反覆驗證穩定性 |

**方法**：對 18 條候選規則計算隔日勝率 · P(≥5%) · 均值；以 **two-proportion z-test**（勝率 / burst）與 **bootstrap 95% CI**（均值差）對 S2 baseline 檢定。

#### 9.4.1 近月 OOS 摘要（Test window）

| 規則 | N | 隔日勝率 | P(隔日≥5%) | 隔日均值 |
|------|---|---------|-----------|---------|
| **watch_setup**（蓄力 · 新） | 6 | **66.7%** | 33.3% | +2.81% |
| **S3 + RV 98–99** | 17 | **64.7%** | 29.4% | +2.77% |
| S3 全部 | 77 | 55.8% | 22.1% | +1.00% |
| S2 Setup core（原 baseline） | 8 | 37.5% | 0.0% | −0.08% |

近月 **S2 樣本極少（n=8）** · 勝率 / burst 差異 **未達 p<0.05**（burst p≈0.09）。

#### 9.4.2 全樣本 · 統計顯著改善（反覆往前驗證）

| 比較 | 指標 | 規則 | Baseline | Δ | p-value | 結論 |
|------|------|------|----------|---|---------|------|
| **S3 + RV 98–99 vs S2** | P(隔日≥5%) | 9.8% | 1.0% | **+8.8pp** | **≈0** | ✅ **顯著** |
| S3 + RV 98–99 vs S3 全部 | P(隔日≥5%) | 9.8% | 10.7% | −0.9pp | 0.58 | 無顯著 |
| watch_setup vs S2 | 隔日勝率 | 45.3% | 44.3% | +1.0pp | 0.38 | 無顯著 |
| base_flat vs S2 | 隔日勝率 | 46.1% | 44.3% | +1.8pp | 0.13 | 無顯著 |

**洞察**：

1. **「命中率」若指隔日 ≥5% burst**：唯一 **大樣本顯著** 提升為 **S3 + RV 98–99** 相對 S2（n=458 vs 3229）。  
2. **蓄力帶（S2 / watch_setup）** 設計目標是 **低波動 · 略抬 burst 尾部**；全樣本 P(≥5%) 仍 **~1%** · 無法靠 tighten excess 拉到統計顯著勝率。  
3. **近月 OOS** 方向正確（watch_setup · S3 RV 勝率 65%+）但 **n 小** · 不作為規則鎖定依據。  
4. **長假 excess**（session_gap>4）應 **停用 excess 門檻** · 改 **watch_setup**（base + streak≥4 + sig∈[−1%, +0.5%]）。

#### 9.4.3 調整後觀測規則（2026-06-30 迭代）

| Track | 舊規則 | 新規則 | 理由 |
|-------|--------|--------|------|
| **蓄力** | S2 Setup core（excess 帶） | **watch_setup** | 長假 robust · 近月勝率方向優於 S2 |
| **S2→D+1 burst** | — | **Rule B** + **三原型 A/B/C** | §8.6 · 日報 `rrg-improving` 頂部 |
| **釋放隔日** | S3 全部 | **S3 + RV 98–99** 優先 | 全樣本 burst **顯著** 優於 S2 |
| **excess 過濾** | 一律套用 | 僅 `session_gap_days ≤ 4` | 避免 6/26→6/29 類失真 |

**日報區塊順序**：Rule B → 原型 A/B/C → watch_setup → S3 全體 → S3 RV98–99。

---

## 10. 實務規則（研究結論 · 未採納）

### 10.1 觀察雷達（2026-06-30 迭代 · §9.4 sweep 驗證）

| 用途 | 規則 | 統計依據 |
|------|------|---------|
| **蓄力 watch** | **watch_setup**：base setup + Improving streak ≥4 + sig∈[−1%, +0.5%] | 近月 OOS 勝率 67%（n=6）· 全樣本 vs S2 未達 p<0.05 |
| **蓄力（相鄰交易日）** | S2 Setup core 僅當 `session_gap_days ≤ 4` | 長假 excess 有效時等同原 S2 |
| **釋放 watch · 隔日** | **S3 + RV 98–99** 優先於 S3 全部 | 全樣本 P(隔日≥5%) **9.8% vs S2 1.0% · p≈0** |
| **釋放 watch · 隔日** | S3 或準 S3（≥4%）· **隔日先看台指** | S3 隔日 mkt_up +1.24% / mkt_down −0.84% |
| **事件 watch** | S0 翻進 · 當日 P(≥5%) ~7% | §6 S0 統計 |

### 10.2 勿過度解讀

| 迷思 | 事實 |
|------|------|
| Setup 隔日穩賺 | mean **≈ 0%** |
| 昨必須跌才會爆 | S3 前日 excess **漲跌各半** |
| RV 95–98 優於 99+ | 回測 **否** · **98–99** 較佳 |
| 6/30 七檔隔日再 +6% | S3 隔日 mean **+0.3%** · 複製 tail 是 overfit |
| 2467 6/30 大漲 → 7/1 追 | 個股 S3 後史 **負期望** |

### 10.3 與採納策略邊界

| 項目 | 採納 `rrg-fresh-mono` | 本研究 lifecycle |
|------|----------------------|------------------|
| 池 | mono tier2 **fresh** | Improving 全階段 |
| 持有 | hold7 D4→D11 | 主要評 **D+1** |
| 用途 | 寄信 · 雷達 | **研究 · 觀測排序** |

---

## 11. 重現方式

```bash
# 生命週期回測 · 輸出 JSON + MD
PYTHONPATH=src .venv/bin/python scripts/run_rrg_improving_lifecycle_backtest.py

# 近月 walk-forward 規則 sweep（train 44d + OOS 22d + rolling 8 fold）
PYTHONPATH=src .venv/bin/python scripts/run_rrg_improving_lifecycle_monthly_sweep.py

# 自訂 burst 門檻
PYTHONPATH=src .venv/bin/python scripts/run_rrg_improving_lifecycle_backtest.py --burst 7
```

**前置**：`stocks.db` 含 `stock_daily_bars` · 成分股同步。缺最新日線時先：

```bash
scripts/daily_sync.sh --market-only
```

---

## 12. 限制與後續

| 限制 | 說明 |
|------|------|
| Universe | 149 ETF 成分 · 非 TW100 全體 |
| 長假 excess | D−1 跳日時 excess 失真 |
| 小樣本 | hot_ctx · 6278 sid_hist · 年度不穩 |
| 產業 beta | RRG 不編碼題材（6/30 AI 鏈順風） |
| 僅 close | 未含 intraday mono fresh |

**後續（research.yaml 候選）**：

- [ ] TW100 universe 重跑  
- [ ] hold5 / hold7 持有期 · 非僅 D+1  
- [ ] 長假專用 excess（僅相鄰交易日）  
- [x] 與 `buy_observation` 池交集報告 · `config/buy_observation.yaml` · `observe_only` 雙軌  
- [x] daily close brief · `scripts/run_rrg_improving_watch_daily.py`
- [ ] intraday `mono_up` fresh 對照實驗（見 `intraday_universe_pool_sweep`）

---

## 13. 相關檔案

| 路徑 | 說明 |
|------|------|
| `src/rrg_rotation.py` | RRG 四象限 · panel |
| `src/rrg_mono_daily_brief.py` | `_feat` · mono tier2 |
| `src/research/backtest/rrg_improving_lifecycle_backtest.py` | 回測核心 |
| `src/research/backtest/rrg_improving_lifecycle_rule_sweep.py` | 近月 walk-forward sweep |
| `scripts/run_rrg_improving_lifecycle_backtest.py` | CLI |
| `scripts/run_rrg_improving_lifecycle_monthly_sweep.py` | 近月規則 sweep CLI |
| `config/buy_observation.yaml` | 買入觀測池 SSOT |
| `docs/intraday-exit-playbook.md` | 盤中出場 · RRG tier |
| `docs/evaluation-contract.md` | 採納評估契約 |

---

*Document version: 2026-06-30 · 對話研究彙整 + `improving_lifecycle_backtest_20260630` 數字 SSOT。*
