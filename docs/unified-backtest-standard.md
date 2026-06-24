# 統一回測標準（跨軌可比較層）

> 繁中說明 · 對外以業界英文術語為準（NAV · Mark-to-market · Drawdown · Sharpe · Bootstrap CI）

**這份文件解決什麼問題**：`strategy_catalog.md` 自己寫明「不可跨軌排名」——本金（5萬/6萬/9萬/連續淨值）、超額定義（逐筆均/訊號日均/區間超額）、複利方式、回測窗口都不同。本文件設計一個**新增的、獨立的「比較層」**，讓六軌可以在同一套假設下被公平比較，**同時不修改、不取代**任何一軌現行的凍結契約。

**關係宣告**：

| 文件/表 | 角色 | 本文件是否取代它 |
|---------|------|------------------|
| [`config/strategy.yaml`](../config/strategy.yaml) | 各軌**契約版**回測規格（本金/槽位/持有期皆為該軌「真實交易方式」） | **否** — 原樣保留 |
| [`evaluation-contract.md`](./evaluation-contract.md) | Per-track backtest spec · 已退役 league table 的歷史記錄 | **否** — 本文件是退役後的**新提案**，非舊 `track_evaluation.py` 復辟 |
| [`strategy_catalog.md`](../supabase/site/strategy_catalog.md) §績效對照 | 對外公開頁 · 契約版數字 | **否** — 維持原樣；本標準的輸出**另開新分節**，並清楚標籤「比較限定版，非操作建議」 |
| `src/strategy_performance_yearly.py` | 現有部分統一層（共同年窗 + `excess_kind` 標籤） | **擴充** — 本文件是它的下一步：把「標籤差異」變成「消除差異」 |

---

## 0. Glossary

| 業界術語 | 說明 | 本文件用法 |
|----------|------|------------|
| **Mark-to-market (M2M)** | 逐日以市價重估未平倉部位 | 所有軌統一改為日內 M2M NAV 序列 |
| **No-compound slot model** | 固定本金/槽位，已實現損益進現金池，不隨權益成長放大下一筆 | 現行 `simulate_slot_portfolio` 採用此模型 |
| **Full-reinvestment model** | 部位大小隨權益成長等比例放大（複利） | 現行 Minervini basket 採用此模型 |
| **Interval excess** | 區間總報酬 − 基準區間總報酬（一個數字，非逐筆平均） | 本標準唯一排名用超額定義 |
| **Per-trade / per-signal-day mean excess** | 逐筆或逐訊號日超額的算術平均 | 降級為**診斷用**指標，不進排名表 |
| **Bootstrap CI** | 對日報酬序列重抽樣估計 Sharpe 等指標的信賴區間 | 用於标注「樣本不足」时的不確定性 |

完整術語規範：[terminology.md](./terminology.md)。

---

## 1. 問題定義（根因，非現象）

`strategy_catalog.md` 列的「不可跨軌排名」原因，拆解到程式層其實是四個獨立根因：

| # | 根因 | 現狀證據 | 影響 |
|---|------|----------|------|
| A | **NAV 序列建構方式不同** | `rrg-mono-hold7` / `rrg-mono-swap-accel` / `vcp-pivot-gate` / `vcp-coil-close` 四軌共用 `simulate_slot_portfolio()`（逐日 M2M，閒置現金不賺不賠）；`00981a-l1h9` 用 `simulate_fixed_slots`（無逐日 NAV，`sharpe_ratio` 在 `strategy_performance_yearly.py` 中硬寫 `None`）；`minervini-sepa-basket` 用 `run_all_broad_momentum_backtests()`（月頻全倉複利，獨立路徑） | Sharpe / MaxDD 三種口徑，00981A 甚至沒有 Sharpe |
| B | **複利假設不同** | 四個槽位軌「不複利」（固定槽位金額）；Minervini「全複利」（月頻等權再平衡） | 同樣的逐筆勝率，複利會放大波動也放大報酬，CAGR 不可比 |
| C | **超額定義不同** | `metrics_json.excess_kind`：00981A=`per_signal_day_mean`，RRG/VCP=`per_period_mean`，Minervini=`interval`（見 `strategy_performance_yearly.py` 各 `_xxx_rows()`） | 三個統計量本質不同，catalog 已明寫「勿直接比大小」 |
| D | **回測窗口不同（連契約規格本身就不同）** | `config/strategy.yaml`：`rrg-mono-swap-accel` 用 `2024-01-01~2026-06-22`；其餘四軌（00981A/hold7/兩條VCP）用 `2026-01-01~2026-12-31`（當年）；00981A 實際資料只從 2025-05-28 起 | 長窗 vs 短窗、partial-year 外推年化（如 2026 YTD 109 個交易日外推出 +919% 年化）放在同一張表會誤導 |

無交易成本模型（commission/tax/slippage）是第五個隱藏問題：`config/strategy.yaml` 六軌 `backtest:` 區塊都沒有費用欄位。換手頻率差很大（swap-accel ~45次/180期 vs Minervini 月頻最低），零成本假設系統性偏惠高頻換倉軌。

---

## 2. 設計原則

1. **新增一層，不竄改舊契約** — 比較層輸出寫入新路徑（`reports/research/_unified/`），不覆寫 `reports/research/{track}/*.json`。
2. **單一資金/複利模型** — 全六軌在比較層統一用**同一個**現有引擎 `simulate_slot_portfolio()`（no-compound slot M2M），不是發明新引擎；00981A 與 Minervini 的訊號/月頻資料先轉成「periods」格式餵入。
3. **單一超額定義** — 只用 interval excess（策略累積% − 基準累積%）排名；逐筆/訊號日均超額保留為診斷欄，標「不可排名」。
4. **顯式窗口，不混排** — 每張比較表必須標 `window_id`；不同 `window_id` 的數字不得放進同一個排名欄。
5. **樣本不足不外推** — 交易日數 < 門檻時，只報區間總報酬，不顯示年化 CAGR。
6. **成本可開關，預設開** — 統一交易成本模型作為比較層的標配假設，並可在 config 關閉以做敏感度分析。
7. **保留診斷指標** — 每軌原生的逐筆超額、勝率、訊號頻率等指標不刪除，只是不進排名欄。

---

## 3. 比較層規格

### 3.1 共同 NAV 引擎

統一改用現有的 `research.backtest.slot_portfolio_metrics.simulate_slot_portfolio()`（已驗證：四軌共用、逐日 M2M、閒置現金計入現金池不生息）作為**唯一**淨值計算引擎。

| 軌 | 現狀 | 接入比較層需要做的事 |
|----|------|----------------------|
| `rrg-mono-hold7` / `rrg-mono-swap-accel` / `vcp-pivot-gate` / `vcp-coil-close` | 已用 `simulate_slot_portfolio` | 無需改動，直接複用 periods |
| `00981a-l1h9` | 用 `simulate_fixed_slots`（無逐日 NAV） | 需確認 `copytrade_backtest.simulate_fixed_slots` 的輸出是否已含 `entry_date`/`exit_date`/`stock_id`/`entry_px`（**待驗證**，若格式相容可直接把 9 槽訊號腿餵進 `simulate_slot_portfolio(n_slots=9, total_capital=90000)`） |
| `minervini-sepa-basket` | 月頻全倉複利籃 · 獨立引擎 | 把每月再平衡的 basket picks 轉成「periods」（entry=當月再平衡日，exit=下月再平衡日，等權），餵入同一引擎；**這是刻意的方法論選擇**——比較層回答「若 Minervini 也用不複利固定槽位來跑，排名會怎樣」，不是「Minervini 實際應該怎麼跑」(後者仍是 `minervini-sepa-basket` 契約版的全複利數字，原樣保留在 catalog) |

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
| `cmp_max_common` | 全六軌都有資料的最長交集窗（目前受 00981A 拖累，約 2025-05-28 ~ 今） | 唯一「嚴格公平」窗，作主排名 |
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

NAV 引擎每次進出場時，從 equity 扣除對應金額（依槽位金額計算，不是按權益）。`config/backtest_standard.example.yaml` 提供 `cost_model.enabled: true/false` 開關，方便做「有成本 vs 無成本」敏感度對照——預期換手最高的 `rrg-mono-swap-accel` 受影響最大。

### 3.6 共同風險指標清單

| 指標 | 公式 | 備註 |
|------|------|------|
| `total_return_pct` | `final_equity/capital - 1` | 已有 |
| `cagr_pct` | `(final/capital)^(252/n_days) - 1` | 樣本 < 120 日留空 |
| `ann_vol_pct` | `std(daily_return) * sqrt(252)` | 已有 |
| `sharpe_ratio` | `mean(daily_return)/std(daily_return) * sqrt(252)`（rf=0） | 已有，套用到全六軌 |
| `max_drawdown_pct` | NAV 序列 peak-to-trough | 目前僅 Minervini 有，比較層全六軌統一補上 |
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
   （讀各軌 periods/legs/monthly_picks → 轉 tidy NAV schema）
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
| **2** | 定義 `cmp_max_common` / `cmp_rolling_252d` 窗口；建 `unified_nav_adapter.py` 把六軌轉成 tidy NAV | 六軌在同一窗口下都有完整逐日 NAV CSV |
| **3** | 套用 3.5 交易成本模型，重跑全六軌 | 產出「有成本 vs 無成本」排名對照，確認換手最高軌排名是否掉落 |
| **4** | Minervini 月頻 basket 轉 no-compound periods 格式 | Minervini 比較層數字與其契約版（全複利）數字並列，差異可解釋 |
| **5** | 建 `strategy_performance_unified` 表 + `league_table_*.md`；`strategy_catalog.md` 新增「標準化比較」分節，與既有「績效對照」明確分隔並互相連結 | 對外頁面同時呈現「契約版」與「比較版」，且讀者能分辨兩者用途不同 |
| **6**（可選） | daily-return bootstrap 95% CI；樣本 < 30 筆時表上標示信賴區間 | 排名差距小於 CI 寬度時，表上標註「差異未達統計顯著」 |

---

## 6. 已知限制（誠實列出，不假裝完全解決）

| 限制 | 說明 |
|------|------|
| 訊號頻率仍不同 | 00981A 是離散訊號日（≈每週數次新進/加碼），RRG/VCP 是日頻篩選，Minervini 月頻再平衡——NAV 化後時間粒度一致了，但「機會密度」本質不同，比較層無法也不該假裝抹平這個事實，只能在診斷欄保留 `n_signal_days` 註記 |
| 比較層數字 ≠ 操作建議 | Minervini 被迫用 no-compound 模型跑，得到的數字不代表你真的會這樣交易它；契約版（全複利）才是它實際的操作規格 |
| 00981A 仍是樣本量最短的拖累項 | `cmp_max_common` 窗口起點被它卡在 2025-05-28，長窗比較（如 Minervini 可回溯 2020）必須排除它並標註 |
| 成本模型是靜態假設 | 0.575% 來回成本是簡化值，未模擬大單滑價、流動性不足個股的實際成交價差 |
| 不解決資金配置問題 | 本標準只回答「公平比較」，不回答「資金怎麼在六軌間分配」——後者是 catalog 自己標註的「下一步：另開組合配置研究」，刻意不在本文件範圍內 |

---

## 7. 新增模組（提案，尚未建立）

| 模組 | 角色 |
|------|------|
| `config/backtest_standard.yaml` | 比較層 SSOT：window_id 定義 · cost_model 開關 · 樣本門檻 |
| `src/research/backtest/unified_nav_adapter.py` | 各軌 periods/legs/monthly_picks → tidy NAV schema 轉換器 |
| `scripts/run_unified_backtest_comparison.py` | 讀 NAV CSV → 套窗口/成本 → 算指標 → 出 league table |
| `src/strategy_performance_unified.py`（可選） | 比照 `strategy_performance_yearly.py` 的表結構，落地 `strategy_performance_unified` 表 |

草稿 schema：[`config/backtest_standard.example.yaml`](../config/backtest_standard.example.yaml)。

---

## 相關文件

| 文件 | 內容 |
|------|------|
| [evaluation-contract.md](./evaluation-contract.md) | 現行 per-track 契約版 backtest spec · 退役歷史 |
| [architecture.md](./architecture.md) | Facts/Regime/Research/Strategy 分層 |
| [terminology.md](./terminology.md) | 術語規範 SSOT |
| [strategy_catalog.md](../supabase/site/strategy_catalog.md) | 對外契約版績效對照頁 |
