# 統一回測標準（跨軌可比較層）

> **Status · 狀態**：**Implemented（比較層 v1）** — 產出 `reports/research/_unified/` · 不取代 `config/strategy.yaml` 契約版。  
> 繁中說明 · 對外以業界英文術語為準（NAV · Mark-to-market · Drawdown · Sharpe · Bootstrap CI）

**這份文件解決什麼問題**：`strategy_catalog.md` 自己寫明「不可跨軌排名」——超額定義（逐筆均/訊號日均/區間超額）、回測窗口、槽位數皆不同。本文件設計一個**新增的、獨立的「比較層」**，讓五軌可以在同一套假設下被公平比較，**同時不修改、不取代**任何一軌現行的凍結契約。

**關係宣告**：

| 文件/表 | 角色 | 本文件是否取代它 |
|---------|------|------------------|
| [`config/strategy.yaml`](../config/strategy.yaml) | 各軌**契約版**回測規格（本金/槽位/持有期皆為該軌「真實交易方式」） | **否** — 原樣保留 |
| [`evaluation-contract.md`](./evaluation-contract.md) | Per-track backtest spec · 已退役 league table 的歷史記錄 | **否** — 本文件是退役後的**新提案**，非舊 `track_evaluation.py` 復辟 |
| `strategy_catalog.md`（已歸檔移除） §績效對照 | 對外公開頁 · 契約版數字 | **否** — 維持原樣；本標準的輸出**另開新分節**，並清楚標籤「比較限定版，非操作建議」 |
| `src/strategy_performance_yearly.py` | 現有部分統一層（共同年窗 + `excess_kind` 標籤） | **擴充** — 本文件是它的下一步：把「標籤差異」變成「消除差異」 |

---

## 0. Glossary

| 業界術語 | 說明 | 本文件用法 |
|----------|------|------------|
| **Mark-to-market (M2M)** | 逐日以市價重估未平倉部位 | 所有軌統一改為日內 M2M NAV 序列 |
| **No-compound slot model** | 固定本金/槽位，已實現損益進現金池，不隨權益成長放大下一筆 | 現行 `simulate_slot_portfolio` 採用此模型 · 比較層唯一模型 |
| **Interval excess** | 區間總報酬 − 基準區間總報酬（一個數字，非逐筆平均） | 本標準唯一排名用超額定義 |
| **Per-trade / per-signal-day mean excess** | 逐筆或逐訊號日超額的算術平均 | 降級為**診斷用**指標，不進排名表 |
| **Bootstrap CI** | 對日報酬序列重抽樣估計 Sharpe 等指標的信賴區間 | 用於标注「樣本不足」时的不確定性 |

完整術語規範：[terminology.md](./terminology.md)。

---

## 1. 問題定義（根因，非現象）

`strategy_catalog.md` 列的「不可跨軌排名」原因，拆解到程式層其實是四個獨立根因：

| # | 根因 | 現狀證據 | 影響 |
|---|------|----------|------|
| A | **NAV 序列建構方式不同** | 四軌槽位策略已用 `simulate_slot_portfolio()`；`00981a-l1h9` 訊號腿接入同一引擎 | Sharpe / MaxDD 口徑已統一於比較層 |
| B | **超額定義不同（契約版）** | `metrics_json.excess_kind`：00981A=`per_signal_day_mean`，RRG/VCP=`per_period_mean`（見 `strategy_performance_yearly.py` 各 `_xxx_rows()`） | 契約版 catalog 仍標「勿直接比大小」；比較層統一為 interval excess |
| C | **回測窗口不同（連契約規格本身就不同）** | `config/strategy.yaml`：`rrg-mono-swap-accel` 用 `2024-01-01~2026-06-22`；其餘四軌（00981A/hold7/兩條VCP）用 `2026-01-01~2026-12-31`（當年）；00981A 實際資料只從 2025-05-28 起 | 長窗 vs 短窗、partial-year 外推年化（如 2026 YTD 109 個交易日外推出 +919% 年化）放在同一張表會誤導 |

無交易成本模型（commission/tax/slippage）是第四個隱藏問題：`config/strategy.yaml` 五軌 `backtest:` 區塊都沒有費用欄位。換手頻率差很大（swap-accel ~45次/180期 vs hold7 最低），零成本假設系統性偏惠高頻換倉軌。

---

## 2. 設計原則

1. **新增一層，不竄改舊契約** — 比較層輸出寫入新路徑（`reports/research/_unified/`），不覆寫 `reports/research/{track}/*.json`。
2. **單一資金/複利模型** — 全五軌在比較層統一用**同一個**現有引擎 `simulate_slot_portfolio()`（no-compound slot M2M）；00981A 訊號腿先轉成「periods」格式餵入。
3. **單一超額定義** — 只用 interval excess（策略累積% − 基準累積%）排名；逐筆/訊號日均超額保留為診斷欄，標「不可排名」。
4. **顯式窗口，不混排** — 每張比較表必須標 `window_id`；不同 `window_id` 的數字不得放進同一個排名欄。
5. **樣本不足不外推** — 交易日數 < 門檻時，只報區間總報酬，不顯示年化 CAGR。
6. **成本可開關，預設開** — 統一交易成本模型作為比較層的標配假設，並可在 config 關閉以做敏感度分析。
7. **保留診斷指標** — 每軌原生的逐筆超額、勝率、訊號頻率等指標不刪除，只是不進排名欄。

---

## 3. 比較層規格

### 3.1 共同 NAV 引擎

比較層使用**單一 notional**（SSOT：`config/backtest_standard.yaml` → `comparison_notional_ntd: 100000`）。各軌僅在 adapter 登記 `n_slots`；`capital_per_slot_ntd = comparison_notional_ntd / n_slots`。排名欄**只看 %**（`interval_excess_pct` · `total_return_pct` · Sharpe · MaxDD），NTD 欄位僅供 NAV 序列尺度，不跨軌比大小。

契約版 per-track JSON 的 `total_capital_ntd` 寫入時取自 **`config/backtest_standard.yaml` → `comparison_notional_ntd`**（`strategy.yaml` 不存本金）。schema 見 `config/slot_backtest_summary.schema.yaml`。

統一改用現有的 `research.backtest.slot_portfolio_metrics.simulate_slot_portfolio()`（已驗證：四軌共用、逐日 M2M、閒置現金計入現金池不生息）作為**唯一**淨值計算引擎。

| 軌 | 現狀 | 接入比較層需要做的事 |
|----|------|----------------------|
| `rrg-mono-hold7` / `rrg-mono-swap-accel` / `vcp-pivot-gate` / `vcp-coil-close` | 已用 `simulate_slot_portfolio` | 無需改動，直接複用 periods |
| `00981a-l1h9` | 用 `simulate_fixed_slots`（無逐日 NAV） | 訊號腿餵進 `simulate_slot_portfolio` · `comparison_notional_ntd` |

輸出統一 schema（tidy long format，每軌每日一行）：

```text
date, strategy_id, equity_ntd, benchmark_equity_ntd, in_market_flag, cash_pct
```

落地：`reports/research/_unified/{strategy_id}_nav_daily.csv`（由新模組 `unified_nav_adapter.py` 產出）。

### 3.2 共同複利模型

比較層**只跑一種模型**：no-compound 固定槽位（即 3.1 引擎本身的行為）。不另外維護「全複利版」雙軌制——維護成本高且容易跟契約版數字混淆。若未來要回答「全複利下排名是否不同」，可作為獨立敏感度分析腳本，不放進主比較表。

### 3.3 共同超額定義

排名表唯一超額欄：

```text
interval_excess_pct = strategy_cum_return_pct(window) − benchmark_cum_return_pct(window)
```

基準 `IX0001` 在同一窗口**全程持有**（不模擬基準的資金利用率），策略的閒置現金拖累就是要呈現的真實成本，不美化。

逐筆均超額 / 訊號日均超額 → 移至「診斷欄」，表頭加註「同軌內比較用，跨軌不可比」。

### 3.4 共同時間窗

| `window_id` | 定義 | 用途 |
|-------------|------|------|
| `cmp_max_common` | 全五軌都有資料的最長交集窗（目前受 00981A 拖累，約 2025-05-28 ~ 今） | 唯一「嚴格公平」窗，作主排名 |
| `cmp_rolling_252d` | 最近 252 個交易日滾動 | 每次刷新自動更新，避免 calendar-year partial-year 外推失真 |
| `cmp_2025_full` | 2025-01-01~2025-12-31 | 對照用；00981A 在此窗為 partial year，CAGR 欄位留空只看區間總報酬 |
| `cmp_2026_ytd` | 2026-01-01~今 | 對照用；同上 partial-year 規則 |

規則：**交易日數 < 120 時不顯示 `cagr_pct`**（只顯示區間總報酬% + 樣本不足標記），避免如目前 catalog 2026 YTD 109 天外推出 +919% 年化的失真留在排名欄。

### 3.5 共同交易成本模型

預設假設（台股現貨）：

```text
買進手續費 0.1425% · 賣出手續費 0.1425% · 賣出證交稅 0.3%
單次完整來回（買+賣）≈ 0.575%
```

NAV 引擎每次進出場時，從 equity 扣除對應金額（依槽位金額計算，不是按權益）。`config/backtest_standard.yaml` 提供 `cost_model.enabled: true/false` 開關，方便做「有成本 vs 無成本」敏感度對照——預期換手最高的 `rrg-mono-swap-accel` 受影響最大。

### 3.6 共同風險指標清單

| 指標 | 公式 | 備註 |
|------|------|------|
| `total_return_pct` | `final_equity/capital - 1` | 已有 |
| `cagr_pct` | `(final/capital)^(252/n_days) - 1` | 樣本 < 120 日留空 |
| `ann_vol_pct` | `std(daily_return) * sqrt(252)` | 已有 |
| `sharpe_ratio` | `mean(daily_return)/std(daily_return) * sqrt(252)`（rf=0） | 比較層全五軌統一 |
| `max_drawdown_pct` | NAV 序列 peak-to-trough | 比較層全五軌統一補上 |
| `calmar_ratio` | `cagr_pct / abs(max_drawdown_pct)` | 新增 |
| `interval_excess_pct` | 見 3.3 | 取代逐筆/訊號日均超額 |
| `win_rate_vs_bench_monthly_pct` | 以月為單位比較策略月報酬 vs 基準月報酬勝率 | 取代逐筆勝率作為排名欄（逐筆勝率仍保留診斷） |
| `n_trading_days` / `n_trades` | 樣本量 | 必列，判斷是否「樣本不足」 |
| `sharpe_ci95_low/high`（可選） | daily return bootstrap 1000 次重抽樣 | Phase 6，樣本 < 30 筆時務必附上 |

### 3.7 穩定性佐證（複用既有方法論）

`rrg-mono-swap-accel` 已有 by-year / by-breadth-zone 拆解佐證做法（見 `reports/research/rrg/20260624_c18_acel3_dls1_stability.json`）。比較層排名表**附帶**子窗口拆解（至少 2025 全年 + 2026 YTD 兩格），避免單一全樣本數字掩蓋「贏在哪個市場環境」的事實——這點 catalog 的「風險與回撤」章節已點出（"2026 上半年廣度偏強勢，槽位三軌 Sharpe 可能偏高"）。

---

## 4. 產出流程（新管線，平行於現行 per-track 管線）

```text
config/backtest_standard.yaml（window_id · cost_model · 門檻）
        ↓
src/research/backtest/unified_nav_adapter.py
   （讀各軌 periods/legs → 轉 tidy NAV schema）
        ↓
reports/research/_unified/{strategy_id}_nav_daily.csv
        ↓
scripts/run_unified_backtest_comparison.py
   （切窗 · 套成本模型 · 算 3.6 全指標 · 可選 bootstrap CI）
        ↓
reports/research/_unified/league_table_{window_id}.json + .md
        ↓
（可選）strategy_performance_unified SQLite/Supabase table → strategy_catalog.md 新分節「標準化比較」
```

不影響現行 `config/strategy.yaml → run_*_backtest.py → reports/research/{track}/*.json` 管線（見 [evaluation-contract.md](./evaluation-contract.md) §2）。

---

## 5. 落地步驟（建議分階段，每階段可獨立驗收）

| Phase | 內容 | 驗收 VFP 句 |
|-------|------|--------------|
| **1** | 驗證 `copytrade_backtest.simulate_fixed_slots` 輸出格式；若相容，把 00981A 接入 `simulate_slot_portfolio` | 00981A 第一次有逐日 NAV、`sharpe_ratio`、`max_drawdown_pct` |
| **2** | 定義 `cmp_max_common` / `cmp_rolling_252d` 窗口；建 `unified_nav_adapter.py` 把五軌轉成 tidy NAV | 五軌在同一窗口下都有完整逐日 NAV CSV |
| **3** | 套用 3.5 交易成本模型，重跑全五軌 | 產出「有成本 vs 無成本」排名對照，確認換手最高軌排名是否掉落 |
| **4** | 建 `strategy_performance_unified` 表 + `league_table_*.md`；`strategy_catalog.md` 新增「標準化比較」分節，與既有「績效對照」明確分隔並互相連結 | 對外頁面同時呈現「契約版」與「比較版」，且讀者能分辨兩者用途不同 |
| **5**（可選） | daily-return bootstrap 95% CI；樣本 < 30 筆時表上標示信賴區間 | 排名差距小於 CI 寬度時，表上標註「差異未達統計顯著」 |

---

## 6. 已知限制（誠實列出，不假裝完全解決）

| 限制 | 說明 |
|------|------|
| 訊號頻率仍不同 | 00981A 是離散訊號日（≈每週數次新進/加碼），RRG/VCP 是日頻篩選，swap-accel 是 5m poll 換倉——NAV 化後時間粒度一致了，但「機會密度」本質不同，比較層無法也不該假裝抹平這個事實，只能在診斷欄保留 `n_signal_days` 註記 |
| 00981A 仍是樣本量最短的拖累項 | `cmp_max_common` 窗口起點被它卡在 2025-05-28；長窗比較（如 swap-accel 可回溯 2024）必須排除它並標註 |
| 成本模型是靜態假設 | 0.575% 來回成本是簡化值，未模擬大單滑價、流動性不足個股的實際成交價差 |
| 不解決資金配置問題 | 本標準只回答「公平比較」，不回答「資金怎麼在五軌間分配」——後者是 catalog 自己標註的「下一步：另開組合配置研究」，刻意不在本文件範圍內 |

---

## 7. 新增模組

| 模組 | 角色 |
|------|------|
| `config/backtest_standard.yaml` | 比較層 SSOT：window_id · cost_model · track adapters |
| `src/research/backtest/unified_nav_adapter.py` | periods → tidy NAV CSV |
| `src/research/backtest/unified_comparison.py` | 切窗 · interval excess · league table |
| `scripts/run_unified_backtest_comparison.py` | CLI 入口 |
| `src/research/sweep_runner.py` | Trial registry · preregistered sweep grid |
| `scripts/run_research_sweep.py` | Sweep CLI 模板 |
| `config/sweep_trial_registry.example.yaml` | Trial JSON schema |

SSOT：`config/backtest_standard.yaml` · schema：`config/slot_backtest_summary.schema.yaml`。

---

## 相關文件

| 文件 | 內容 |
|------|------|
| [evaluation-contract.md](./evaluation-contract.md) | 現行 per-track 契約版 backtest spec · 退役歷史 |
| [architecture.md](./architecture.md) | Facts/Regime/Research/Strategy 分層 |
| [terminology.md](./terminology.md) | 術語規範 SSOT |
| strategy_catalog.md（已歸檔移除） | 對外契約版績效對照頁 |
