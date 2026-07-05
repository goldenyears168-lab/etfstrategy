# RRG Pre-release Funnel Lanes 中文解說

> **SSOT 設定檔**：[`config/rrg_pre_release_funnel_lanes.yaml`](../config/rrg_pre_release_funnel_lanes.yaml)  
> **Layer**：research（研究層 · 未採納至 `config/strategy.yaml`）  
> **回測模組**：`src/research/backtest/rrg_pre_release_funnel_lanes.py`  
> **Runner**：`scripts/run_rrg_pre_release_funnel_backtest.py`  
> **產物**：`reports/research/rrg/pre_release_funnel_lanes_YYYYMMDD.{md,json}`  
> **前置研究**：[`docs/rrg-improving-lifecycle-research.md`](./rrg-improving-lifecycle-research.md)

---

## 1. 這份設定檔在做什麼？

本設定檔是 **RRG Improving/Lagging 軌道** 上、**pre-release（pre-burst · 爆發前）** 隔日交易的 **漏斗車道（funnel lanes）登錄表**。

研究目標不是找「一條全年適用的寬鬆策略」，而是：

1. 定義多條 **條件嚴格、可復刻** 的獨立車道（lane）  
2. 每條車道在 signal-day 收盤前即可判定（**PIT · 時點一致性**）  
3. 以 **隔日勝率** 與 **隔日均報酬** 為主要 KPI  
4. 多條低頻高品質車道合併，提高 **可交易覆蓋日** 而不稀釋整體 edge

概念上類似「30 種獨立微策略 × 各 ~10 筆/年 → 合起來一年有更多可用交易日」，而非單一複利曲線。

---

## 2. 研究邏輯（為什麼是 pre-release？）

### 2.1 從 lifecycle 到漏斗

[`rrg-improving-lifecycle-research.md`](./rrg-improving-lifecycle-research.md) 將 Improving 象限內標的分為 S0–S4 等生命週期階段。其中：

| 階段 | 語意 | 隔日交易適性 |
|------|------|-------------|
| **S2 Setup core** | 蓄力帶 · 4 日 improving · 價格未 chase | 低頻 · 略優於母池 |
| **S3 burst** | 當日已 ≥5% 大漲 | **事後** · 隔日仍受台指方向影響 |
| **Pre-release** | 尚未 burst · 仍 <5% | **可交易** · 找「大漲前一日跡象」 |

**Pre-release（pre-burst）** 指：標的已在 Improving/Lagging 修復軌道上，但 **當日漲幅尚未達 burst 門檻（5%）**。這是「明天可能大漲」的研究框架，而非在 burst 當日事後歸納。

### 2.2 評估標的與報酬定義

| 欄位 | 意義 |
|------|------|
| **signal-day（信號日 D）** | 所有因子在 D 日收盤可算完 |
| **outcome `nxt_ret`** | D 收盤買入 → D+1 收盤賣出之報酬率（%） |
| **win（隔日勝）** | `nxt_ret > 0` |
| **mean（隔日均報酬）** | 該車道所有命中事件的 `nxt_ret` 平均 |

**PIT 原則**：因子僅用 `date ≤ D` 的資料。例如 `bench_today_ret` 是 **當日台指漲幅**，在 D 收盤後可知；**不可用** `bench_nxt_ret`（隔日台指）作為進場條件。

### 2.3 兩層車道設計

| Tier | ID 範圍 | 設計目的 | 典型門檻 |
|------|---------|----------|----------|
| **strict（嚴格層）** | R01–R30 | 高勝率 · 低重疊 · 可復刻精品 | 勝率 ≥60% · 均報酬 ≥+0.8% |
| **coverage（覆蓋層）** | C01–C17 | 補不同 regime · 提高日曆覆蓋 | 勝率 ≥55% · 均報酬 +1.0%～+3.0% |

嚴格層求 **品質**；覆蓋層求 **互補**（不同因子組合、不同台指環境、不同 streak 階段），並控制與 strict 的 **Jaccard 重疊度**。

---

## 3. 母池（universe）怎麼定？

設定檔 `universe` 區塊定義 **哪些 stock-day 進入搜尋母池**：

```text
(end_q ∈ {improving, lagging})
∧ sig_ret < 5%                                    ← 尚未 burst
∧ ( end_q = improving
    ∨ ( end_q = lagging ∧ (disp_contract ∨ sig_ret > 0.5%) ) )
```

白話解讀：

1. **已在 RRG 修復/落後軌道**：象限為 Improving 或 Lagging  
2. **當日未大漲**：`sig_ret < 5%`，排除 S3 burst 當日  
3. **Lagging 額外閘門**：若仍在 Lagging，需有 **disp 收斂** 或 **當日小漲 >0.5%**，避免純弱勢無動能標的

資料來源：

- `build_lifecycle_events()`：從 RRG panel 掃描全監測名單（ETF 成分股 watchlist）  
- `enrich_lifecycle_events()`：附加 PIT 特徵（如 `prev_end_q`、個股 burst 史績）

母池基線（2015–2026 約 11 年）：隔日勝率約 **46%**、均報酬约 **+0.06%**。所有車道 edge 皆相對此母池。

---

## 4. 因子原子（atoms）字典

車道由 **atoms（原子條件）** 的 **AND 組合** 構成。`rule_atoms` 列表即完整可復刻規則。

### 4.1 台指環境（β 代理）

| Atom | 中文 | 定義 |
|------|------|------|
| `b2` | 台指當日 ≥+2% | `bench_today_ret ≥ 2%` · 強勢大盤日 |
| `b1` | 台指當日 ≥+1% | 溫和偏多環境 |
| `b0` | 台指當日 ≥0% | 非下跌日 · 門檻最寬 |

> **為何台指重要？** S3 burst 隔日報酬高度依賴大盤方向。pre-release 研究發現 **強勢台指日（b2）** 是最高頻出現的 gating 因子。

### 4.2 RRG 軌道與位置

| Atom | 中文 | 定義 |
|------|------|------|
| `imp` | 當日 Improving | `end_q == improving` |
| `plag` | 前日 Lagging | `prev_end_q == lagging` · 剛從弱勢區轉出 |
| `elag` | 當日 Lagging pre-flip | `end_q == lagging` · 仍在落後象限但有 pre-burst 跡象 |
| `disp` | 離散度收斂 | `disp_contract` · 4 日軌跡位移 **Δdisp < −0.1** |
| `rv98` | RS-Ratio 98–99 | `RS-Ratio ∈ [98, 99)` · Improving 尾段、即將進 Leading |
| `base` | Base setup | 4 日皆 improving · `up_right` · disp 適中 · 非 mono |
| `s2` | S2 setup core | base setup + excess 蓄力帶 + 排除 chase/深跌 |

### 4.3 Improving streak（連續天數）

| Atom | 中文 | 定義 |
|------|------|------|
| `st1` | 第 1 天 | `improve_streak == 1` · 剛翻進 Improving |
| `st13` | 第 1–3 天 | 修復早段 |
| `st46` | 第 4–6 天 | 蓄力中段 · 常與 base setup 重疊 |
| `st7p` | ≥7 天 | 長 streak · 晚期蓄力 |
| `no23` | 排除 streak 2–3 | 統計上隔日表現偏弱的中間段 |
| `s0` | Fresh flip | `s0_fresh_flip` · 前日非 improving、當日 improving |

### 4.4 價格形態（signal-day 漲幅）

| Atom | 中文 | 定義 |
|------|------|------|
| `dip` | 微幅回檔 | `sig_ret ∈ [−2%, 0%]` |
| `flat` | 平盤帶 | `sig_ret ∈ [−1%, +1%]` |
| `q35` | 準 burst 3–5% | 動能升溫但未達 5% |
| `q45` | 準 burst 4–5% | 更接近 burst 的窄帶 |
| `ex` | 落後大盤蓄力 | `excess ∈ [−2%, −0.3%]` · 相對前日大盤略跑輸 |

### 4.5 個股史績與交易日

| Atom | 中文 | 定義 |
|------|------|------|
| `hist` | 個股 burst 史正向 | 該股過去 ≥3 次 S3 burst，且歷次 burst **隔日均報酬 > 0** |
| `gap1` | 連續交易日 | `session_gap_days == 1` · 排除長假後首日的 excess 基準失真 |

---

## 5. 車道（lanes）結構說明

每一條 lane 在 YAML 中含：

| 欄位 | 用途 |
|------|------|
| `id` | 唯一代號 · R01–R30（strict）或 C01–Cxx（coverage） |
| `tier` | `strict` 或 `coverage` |
| `rule_atoms` | 原子條件列表 · **AND 邏輯** |
| `label` | 人類可讀 rule string · 如 `b2+disp+base+st46` |
| `description` | 英文簡述 · 供程式與報告引用 |
| `regime_tag` | 該車道所屬 **regime 類型** · 便於分組解讀 |

### 5.1 regime_tag 分類（strict 層摘要）

| regime_tag | 含義 | 代表車道 |
|------------|------|----------|
| `bench_strong` | 強台指 + disp↓ + 個股史績 | R01 |
| `lagging_pre_flip` | Lagging 軌道 pre-flip + RV98 | R02 |
| `early_improving` | 第 1 天 improving + 準 burst | R03, R08 |
| `base_coil` / `base_broad` | Base setup 蓄力 · 中 streak | R04, R18 |
| `plag_recovery` | 前日 lagging 轉出 + 回檔 | R06, R19 |
| `rv98_edge` | RS-Ratio 貼 100 門檻 | R05, R10, C01 |
| `lagging_track` | Lagging 相關覆蓋 | C04, C11, C17 |
| `neutral_bench` | 台指非強勢但仍有效 | R11, R17 |

### 5.2 strict 層 R01–R30 一覽

| ID | Rule | 白話摘要 |
|----|------|----------|
| R01 | b2+disp+hist+dip+ex+gap1 | 強台指 · disp↓ · 史績好 · 小跌 · 跑輸大盤蓄力 |
| R02 | b2+elag+dip+rv98+gap1 | Lagging pre-flip · RV98 · 強台指 · 小跌 |
| R03 | b2+st1+q45+gap1 | 第 1 天 improving · 準 burst 4–5% · 強台指 |
| R04 | b2+disp+base+st46+hist+gap1 | Base 蓄力 · 中 streak · disp↓ · 史績好 |
| R05 | b2+disp+base+st46+rv98+gap1 | 同上 + RV98 貼 Leading |
| R06 | b2+disp+hist+plag+dip+gap1 | 前日 lagging · 史績好 · 小跌 · disp↓ |
| R07 | b2+disp+plag+ex+rv98 | 前日 lagging · excess 蓄力 · RV98 |
| R08 | b1+st1+hist+q45+gap1 | 第 1 天 · 準 burst · 史績好 · 台指 ≥1% |
| R09 | b2+st13+hist+plag+q35+gap1 | 早 streak · 前日 lagging · 準 burst 3–5% |
| R10 | b2+disp+st46+hist+rv98+gap1 | 中 streak · disp↓ · RV98 · 史績好 |
| R11 | b0+disp+st1+hist+q35+gap1 | 台指非跌 · 第 1 天 · 準 burst |
| R12 | b2+disp+base+st46+dip | Base 蓄力 · 小跌（無 gap1 要求） |
| R13 | b2+disp+hist+plag+flat+rv98 | 平盤 · 前日 lagging · RV98 |
| R14 | b2+st13+hist+ex | 早 streak · excess 蓄力 · 史績好 |
| R15 | b2+dip+flat+ex+rv98+gap1 | 平盤小跌 · excess · RV98 |
| R16 | b2+st13+hist+q45+gap1 | 早 streak · 準 burst 4–5% |
| R17 | b0+disp+base+st46+q35+gap1 | Base · 準 burst · 台指非跌 |
| R18 | b2+disp+base+st46 | **樣本最大** · Base 蓄力寬口 |
| R19 | b2+disp+hist+plag+flat+gap1 | 前日 lagging · 平盤 · disp↓ |
| R20 | b1+st46+hist+ex+rv98+gap1 | 中 streak · excess · RV98 |
| R21 | b2+plag+elag+dip+flat+gap1 | Lagging 軌道 · 小跌平盤 |
| R22 | b2+disp+st46+hist+gap1 | 中 streak · disp↓ · 史績好 |
| R23 | b2+disp+st1+hist | 第 1 天 · disp↓ · 史績好 |
| R24 | b2+hist+dip+ex+rv98 | 小跌 · excess · RV98 |
| R25 | b2+st46+hist+flat+gap1 | 中 streak 平盤 · 史績好 |
| R26 | b2+disp+base+flat+rv98 | Base 平盤 · RV98 |
| R27 | b0+q35+ex+no23 | 準 burst · excess · 排除 streak 2–3 |
| R28 | b1+st13+hist+plag+ex+gap1 | 早 streak · 前日 lagging · excess |
| R29 | b2+hist+ex+gap1 | excess 蓄力 · 史績好 · 較寬 |
| R30 | b1+disp+base+st46+hist+gap1 | Base · 台指 ≥1% · 史績好 |

### 5.3 coverage 層 C01–C17 一覽

覆蓋層 **刻意不要求 b2**，或組合不同 streak / RV98 / plag，以補 strict 未觸發的 regime。

| ID | Rule | 備註 |
|----|------|------|
| C01 | disp+gap1+imp+q45+rv98 | primary · RV98 + 準 burst |
| C02 | disp+gap1+q35+rv98+st13 | primary · 早 streak + RV98 |
| C03 | b1+disp+gap1+hist+st1 | primary · 第 1 天 + 史績 |
| C04 | b1+ex+flat+gap1+plag+st13 | primary · lagging 軌道 |
| C05 | b2+rv98+st1 | primary · 強台指 + RV98 + 第 1 天 |
| C06 | b2+disp+hist+plag+q35+rv98 | primary · plag + 準 burst |
| C07 | b2+disp+ex+hist+imp | primary · excess + improving |
| C08 | b2+disp+imp+no23+q35+rv98 | primary · 排除 streak 2–3 |
| C09 | disp+gap1+imp+q35+rv98 | extended · 門檻略放寬 |
| C10 | disp+ex+flat+gap1+hist+st1 | extended |
| C11 | disp+gap1+plag+q35+st13 | extended · lagging track |
| C12 | b0+disp+ex+flat+hist+st1 | extended · 台指非跌 |
| C13 | b1+dip+flat+gap1+plag+st13 | extended |
| C14 | b0+disp+gap1+hist+q45+st7p | extended · 長 streak ≥7 |
| C15 | b1+flat+gap1+hist+plag+st13 | extended |
| C16 | b1+disp+flat+gap1+plag+st13 | extended |
| C17 | b1+dip+disp+gap1+plag+st13 | extended · 均報酬約 +1.0% 下限 |

> **primary vs extended**：discovery 兩階段搜尋。primary 滿足 `n≥20 · win≥55% · mean 1–3%`；extended 放寬至 `n≥15 · mean≥0.8%` 以補足 lane 數量。

---

## 6. discovery 區塊（自動搜尋參數）

若執行 `--write-coverage`，程式依下列參數 **重新搜尋** 覆蓋層車道並寫回 YAML：

| 參數 | 值 | 意義 |
|------|-----|------|
| `win_min` | 0.55 | 覆蓋層最低隔日勝率 55% |
| `mean_min` / `mean_max` | 1.0 / 3.0 | 均報酬區間 +1%～+3% |
| `n_min` | 20 | primary 最少樣本 |
| `n_min_extended` | 15 | extended 最少樣本 |
| `mean_min_extended` | 0.8 | extended 均報酬下限 |
| `jaccard_max` | 0.35 | 與 strict 層最大重疊 |
| `jaccard_max_vs_coverage` | 0.45 | 覆蓋層彼此可略放寬 |
| `target_union_days_per_year` | 50–80 | **目標** 合併日曆覆蓋（目前未達） |
| `min_combo_size` / `max_combo_size` | 2–6 | 每條 lane 原子數範圍 |

### 6.1 Jaccard 重疊度

兩條 lane 的命中事件集合 A、B：

```text
Jaccard(A, B) = |A ∩ B| / |A ∪ B|
```

要求 **≤0.35** 表示兩條 lane 不能只是同一批標的的微調，必須是 **不同 regime 的獨立假設**。

---

## 7. 回測指標怎麼讀？

Runner 產出的 JSON / MD 報告含：

| 指標 | 定義 |
|------|------|
| **n_events** | 該 lane 命中次數（stock-day 筆數） |
| **n_calendar_days** | 命中涉及的不重複 **交易日** 數 |
| **days_per_year** | 日曆天 / 年 |
| **win_rate** | 事件層隔日勝率 |
| **mean_pct** | 事件層隔日均報酬 |
| **loss5_rate** | 隔日跌幅 ≤−5% 比例 |
| **year_table** | 分年 n / win / mean |
| **day_win_rate**（union） | 以 **日** 為單位：當日所有命中標的等權平均後，隔日報酬 >0 的比例 |
| **day_mean_pct**（union） | 日層等權平均隔日報酬 |

### 7.1 最新回測摘要（2026-07-01 執行）

| 合併範圍 | 日曆天/年 | 日層勝率 | 日層均報酬 |
|----------|-----------|----------|------------|
| **僅 strict（R01–R30）** | ~15 | ~70% | ~+0.95% |
| **strict + coverage（47 條）** | ~32 | ~61% | ~+0.69% |

解讀：

- 嚴格層 **品質高但低頻**（約每月 1–2 個交易日有任一 lane 觸發）  
- 加入覆蓋層後 **日曆覆蓋約翻倍**，日層均報酬略降但仍為正  
- 距 discovery 目標 **50–80 天/年** 仍有差距 · 需放寬 Jaccard 或 mean 下限才能再擴

---

## 8. 如何使用

```bash
# 完整回測 · 輸出 JSON + MD 報告
PYTHONPATH=src python3 scripts/run_rrg_pre_release_funnel_backtest.py

# 重新搜尋覆蓋層並寫回 YAML（會改寫 formatting）
PYTHONPATH=src python3 scripts/run_rrg_pre_release_funnel_backtest.py --write-coverage

# 單元測試
PYTHONPATH=src python3 -m unittest tests.test_rrg_pre_release_funnel_lanes -v
```

報告中路徑範例：`reports/research/rrg/pre_release_funnel_lanes_20260701.md`  
內含 **每條 lane 的分年表**（year · n · days · win · mean）。

---

## 9. 實務解讀與限制

### 9.1 可復刻性

- 所有 `rule_atoms` 在 **D 日收盤後** 可從 RRG panel + 價格計算  
- 不包含 burst 當日事後資訊 · 不包含隔日台指  
- `hist` 使用 **該股過去 burst 的隔日報酬** · 嚴格 PIT 滾動計算

### 9.2 非 live gate

本設定檔屬 **research layer**。結果未採納至 `config/strategy.yaml` 前，**不得** 作為下單層（order layer）自動執行規格。

### 9.3 長假與 excess

`excess` 基準為 **前一交易日** 大盤報酬。長假後首個交易日（`gap1` 過濾為 false）excess 解讀需謹慎 · 許多 strict lane 要求 `gap1` 正是為此。

### 9.4 覆蓋目標 vs 品質權衡

在 **mean ≥1%** 且 **Jaccard ≤0.35** 約束下，自動搜尋僅穩定產出 **~17 條** coverage lane（非原目標 20–30）。這反映 pre-release 母池中 **高均報酬 + 低重疊** 的組合本身稀疏。

若優先 **日曆覆蓋**，可考慮：

- 放寬 `jaccard_max_vs_coverage`  
- 降低 `mean_min` 至 0.5–0.8%  
- 增加 `b0` / 非 b2 的 neutral regime lane

若優先 **隔日品質**，維持 strict 層 R01–R10 即可。

---

## 10. 與其他模組的關係

```text
rrg_rotation.py / rrg_mono_daily_brief.py
        ↓
rrg_improving_lifecycle_backtest.py   ← lifecycle S0–S4 母研究
        ↓
rrg_improving_s3rv_executable.py      ← pre-release conviction score（連續評分）
        ↓
rrg_pre_release_funnel_lanes.py       ← 本設定檔 · 離散 lane 登錄 + 回測
        ↓
reports/research/rrg/pre_release_funnel_lanes_*.md
```

- **Conviction score**（連續 0–100 分）適合倉位分檔  
- **Funnel lanes**（離散 AND 規則）適合「條件一致、可復刻、可命名」的微策略組合

兩者共用同一 pre-release 母池，互補而非取代。

---

## 11. 名詞對照（terminology）

| English | 中文 | 本文件用法 |
|---------|------|------------|
| Pre-release / pre-burst | 爆發前 / 蓄力帶 | 信號日 `sig_ret < 5%` |
| PIT | 時點一致性 | 無前視偏差 |
| Funnel lane | 漏斗車道 | 一條 AND 規則 + 回測統計 |
| Regime tag | 環境標籤 | 描述 lane 所屬市場型態 · 非 regime layer 四軸診斷 |
| Jaccard | Jaccard 重疊度 | lane 獨立性度量 |
| Union | 合併覆蓋 | 任一 lane 命中即計入該日 |

完整術語規範見 [`docs/terminology.md`](./terminology.md)。

---

*文件版本：對應 `config/rrg_pre_release_funnel_lanes.yaml` · `rrg-pre-release-funnel-v1` · 2026-07-01*
