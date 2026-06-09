# 量化交易系統架構（Quant Trading System Architecture）

> **混合資料源**（TEJ · FinMind · Yahoo · 投信官網/EZMoney）+ **Python** + **本地 SQLite**（`data/stocks.db`）+ **方案 C 排程**  
> 研究主軸：**ETF 持股行為 → 投資決策引擎**（詳見 [PRD.md](./PRD.md) v0.3）  
> 執行：**人工下單**（富邦 Neo API 自動化為 Out of Scope）

## 專案目錄

```
股票研究/
├── src/           # 所有 Python 模組
├── scripts/       # daily_sync、weekly_sync、.command
├── docs/          # PRD、本檔、daily-operations
├── data/          # stocks.db
├── logs/
├── tests/
├── .cursor/skills/
└── reports/       # 每日產物（gitignore）
```

## 系統流程總覽

```
┌─────────────────────────────────────────────────────────┐
│  External Data Sources（批次 Ingest，Analyze 只讀 DB）      │
│  TEJ │ FinMind │ Yahoo │ EZMoney/凱基/群益/野村官網 │ 新聞* │
└────────────────────────────┬────────────────────────────┘
                             ↓
                      Data Layer
                   (stocks.db · Raw)
                             ↓
                      Research Layer
          ┌──────────────────┴──────────────────┐
          │  A. ETF 行為引擎（✅ P0）              │
          │  B. 雙引擎 + Score + Rule（✅ P1–P2） │
          │  C. 多因子因子表（📋 長期／可選）        │
          └──────────────────┬──────────────────┘
                             ↓
                      Portfolio Layer
                   （pm_watchlist · portfolio_weights，✅）
                             ↓
                      Execution Layer
                   （人工下單，Out of Scope 自動化）
                             ↓
                      Analytics Layer
                   （④ Signal Review v0.3 · §0 Flow；績效對帳 📋）

* 新聞 / LLM：僅 Research Universe（≤20 檔），非全市場
```

### 營運排程（方案 C · 對應 Data / Research 觸發）

| 排程 | slug | 時間 | Job（概念） | 腳本入口 |
|------|------|------|-------------|----------|
| ① 執行評估（原早盤風險哨） | `execution-eval` | 週一至五 08:30 | `ingest_market_risk` + **執行快照評估** | `scripts/0830執行評估.command` → `daily_sync.sh --market-only`；見 [execution-eval-PRD.md](./execution-eval-PRD.md) |
| ② 收盤持股雷達 | `evening-holdings` | 週一至五 16:30 | `ingest_holdings` + `report_behavior` | `scripts/1630收盤雷達.command` → `daily_sync.sh --holdings-only` |
| ③ 週日深度補庫 | `weekly-deep` | 週日 20:00 | `ingest_weekly_slow` | `scripts/2000週日補庫.command` → `weekly_sync.sh` |
| ④ 策略回顧 | `signal-review` | 隨時 | `analyze_signal_outcomes` | `scripts/策略回顧.command` → `signal_review.py`（見 [signal-review-PRD.md](./signal-review-PRD.md)） |

排程載體：**Mac `launchd` 或手動雙擊 `.command`**；log 寫入 `logs/`。

---

| Layer | Job 名稱 | 任務 | 目的 | Input | Output | Dependency | 設計依據 | 現況 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **Data Layer** | `ingest_market_risk`（①） | 同步基準日線、科技風險、可選 ETF 法人 | 開盤前風險底座；**不**依賴當日官網持股 | • TEJ：6 ETF + IX0001 + IR0002（**FinMind 備援**）<br>• Yahoo：TSM ADR、SOX/SMH<br>• FinMind：台指期/電子期、spread<br>• `.env`：`TEJ_API_KEY`、`FINMIND_TOKEN`<br>• `ENABLE_FINMIND_SIGNAL`（可選） | • `daily_bars`<br>• `tech_risk_daily_snapshot`<br>• `etf_daily_signal_snapshot`（opt）<br>• `logs/daily_sync_YYYYMMDD.log` | • `src/query_stock_prices.py` 等<br>• `scripts/daily_sync.sh`（`PYTHONPATH=src`） | CRSP 可追溯原則；**Ingest/Analyze 分離**（PRD） | ✅ 已實作 |
| **Execution Layer** | `evaluate_execution_snapshot`（①·E0.2） | 執行評估：ref/qty/pre-trade；可重複 | **Fast Book**；研究凍結於收盤 | • `pm_watchlist` / `portfolio_weights`<br>• IPS<br>• 可選試撮價 `--prices` | • `order_intents`<br>• `reports/*_execution_eval.md` | • `execution_eval.py`（規劃）<br>• `order_intent_engine.py` | 見 [execution-eval-PRD.md](./execution-eval-PRD.md) | 📋 E0.2 規格 |
| **Data Layer** | `ingest_holdings`（②） | 四源官網持股寫入 DB；產出變化與意圖報告 | 建立 ETF 資金行為的 **慢變量** 底座 | • EZMoney（統一）<br>• 凱基 / 群益 CFWeb / 野村 ETFAPI 官網<br>• 7 檔 ETF 代碼清單<br>• 對齊 cohort 規則 | • `etf_holdings`<br>• `etf_holdings_meta`<br>• **`flow_events`**（intent 後快照）<br>• `investment_scores` / `pm_watchlist` / `portfolio_weights`（`RUN_SCORE_ENGINE=1`）<br>• log：`--changes --intent` | • `src/sync_etf_holdings.py`<br>• `src/holdings_research.py`<br>• `src/sync_flow_events.py`<br>• `src/pipeline_evening.py`<br>• `scripts/daily_sync.sh` | 基金持股披露節奏；snapshot Skip 設計 | ✅ 已實作 |
| **Data Layer** | `ingest_weekly_slow`（③） | Beta、基本面、成分股批次 | 週期資料與平日 API **解耦** | • FinMind / Yahoo / TEJ（依腳本）<br>• 持股聯集 universe<br>• `RUN_STOCK_MARKET_SYNC=1` | • `stock_beta`<br>• **`stock_daily_bars`**<br>• **`stock_institutional_daily`**<br>• **`stock_fundamental`**<br>• `logs/weekly_sync_YYYYMMDD.log` | • `scripts/weekly_sync.sh`<br>• `src/sync_stock_beta.py`<br>• `src/sync_fundamentals.py`<br>• `src/sync_stock_market_daily.py` | Fama-French / MSCI 需穩定基本面；**勿**塞進平日早盤 | ✅ 已實作 |
| **Data Layer** | `ingest_intraday`（獨立） | 1 分 K、盤中訊號 | 執行輔助；**不**進方案 C | • 即時行情 API | • `intraday_1m_bars`<br>• `intraday_signals` | • `src/intraday_monitor.py` | 演算法交易前控；與洗盤/當沖防線在 Execution | 🔶 有表有腳本；非每日主鏈 |
| **Research Layer** | `compute_etf_behavior` | L1–L6、Rotation、Position Intent、註解 | 將持股 diff 轉為 **可解釋行為訊號** | • `etf_holdings`<br>• `investment_themes`（靜態）<br>• 對齊 cohort | • 終端 / log 報告<br>• （記憶體）`StockSignal` | • `src/signal_engine.py` 等<br>• **無 API** | 機構持股流研究；非單日 K 因子 | ✅ P0 |
| **Research Layer** | `build_research_universe` | Money Flow Top10 ∥ Event Top10 → 15–20 檔 | 避免「加碼小但事件大」漏標 | • 行為訊號<br>• `catalyst_events`（opt）<br>• 監控池（持股聯集） | • Universe 清單<br>• 事件排名 | • `research_universe.py`<br>• `event_ranking.py` | 雙引擎平行（PRD §7） | ✅ P1 |
| **Research Layer** | `compute_investment_score` | 五維子分 + 總分 + Rule 觀察名單 A/B/C | **規則決策**；禁止 LLM 評級 | • DB：行為、L7、L8/L8.5、Risk<br>• `tech_risk`、`stock_beta` | • `investment_scores`<br>• `pm_watchlist` / `portfolio_weights` | • `score_engine.py`<br>• `expectation_engine.py`<br>• `portfolio_engine.py`<br>• **只讀 DB** | `WEIGHT_CATALYST=0`（子分保留）；**單日法人不直接進 A** | ✅ P2–P3 |
| **Research Layer** | `generate_memo` | Top10 敘事備忘（Bull/Bear/Why） | AI **解釋**不 **決策** | • `investment_scores`<br>• 結構化 JSON | • `research_memos` / `reports/*.md` | • `investment_memo.py`<br>• LLM（`RUN_MEMO=1` opt） | Memo 邊界（PRD §11） | 🔶 P4（預設關） |
| **Research Layer** | `compute_factors`（長期可選） | 價值、動能、品質、成長、風險因子 | 全市場或多因子擴充時啟用 | • `stock_daily_bars`*<br>• `stock_fundamental`* | • `factor_scores`*<br>• `factor_rankings`*<br>• `composite_scores`* | • Factor Engine*<br>• Ranking Engine* | Fama-French 5/6 · MSCI Factor | 📋 未啟動 |
| **Portfolio Layer** | `generate_watchlist_targets` | 觀察名單 → 目標權重 / 再平衡草案 | 研究結果轉 **可執行配置** | • `investment_scores`<br>• `strategy_config`<br>• 集中度約束 | • **`pm_watchlist`**<br>• **`portfolio_weights`**<br>• *`portfolio_targets`* / *`rebalance_versions`* | • `pm_watchlist.py`<br>• `portfolio_engine.py` | MPT / 多因子組合（原架構保留） | ✅ 核心已建 |
| **Execution Layer** | `execute_with_risk_controls` | 風控、下單、成交追蹤 | 閉環交易（自動化） | • `portfolio_targets`*<br>• 券商 API 持倉<br>• 即時行情<br>• `risk_config` | • `execution_orders`*<br>• `execution_fills`*<br>• `risk_snapshots`* | • Risk Engine*<br>• 券商 API* | Pre-Trade Controls；**擋洗盤/當沖追價** | ❌ Out of Scope |
| **Analytics Layer** | `analyze_signal_outcomes`（④） | 訊號事後歸因、Paper 10 萬每日全換、§0 Flow Attribution | 驗證 rule 與分桶 | • `flow_events`<br>• `pm_watchlist`<br>• `portfolio_weights`<br>• `stock_daily_bars` | • `reports/*_signal_review.md` | • `signal_review.py`<br>• `flow_attribution.py` | IC · Event study · Boss Gate H+3/H+5 | ✅ v0.3 |
| **Analytics Layer** | `analyze_performance` | 對帳、績效、執行品質、策略監控 | 驗證策略與成本 | • 成交 / 持倉<br>• Benchmark<br>• 交易成本 | • `analytics_performance`*<br>• `reconciliation_logs`*<br>• `daily_strategy_reports`* | • Analytics Engine*<br>• Execution Layer | GIPS · Brinson Attribution | ❌ 未建 |

---

## 資料來源矩陣（Data Layer）

| 來源 | 用途 | 腳本 | 寫入表 | 排程 | 狀態 |
|------|------|------|--------|------|------|
| **TEJ API** | ETF/指數日線（EWPRCD/EWIPRCD）；失敗時 FinMind 備援 | `query_stock_prices.py` | `daily_bars`（`source=tej`/`finmind`） | ① | ✅ 主源 + 備援 |
| **Yahoo Finance** | TSM ADR、SOX/SMH；Beta 備援 | `sync_tech_risk_context.py`、`sync_stock_beta.py` | `daily_bars`、`tech_risk_*`、`stock_beta` | ①③ | ✅ |
| **FinMind** | ETF 三大法人；期貨；成分股價+法人；基本面 | `sync_etf_signal.py`、`sync_stock_market_daily.py`、`sync_fundamentals.py` 等 | `etf_daily_signal_snapshot`、**`stock_daily_bars`**、**`stock_institutional_daily`**、**`stock_fundamental`** | ①②③ | 🔶 ETF 法人預設關；成分股 `RUN_STOCK_MARKET_SYNC=1` |
| **EZMoney** | 統一投信 ETF 持股 | `sync_etf_holdings.py` | `etf_holdings` | ② | ✅ |
| **凱基 / 群益 / 野村 官網** | 各 ETF 持股 HTML/API | 同上 | 同上 | ② | ✅ |
| **新聞 API / RSS** | L7 催化（Universe only） | `catalyst_engine.py`、`sync_catalyst_news.py` | `catalyst_events` | ② 後段 opt | 🔶 `RUN_CATALYST_ENGINE` / `RUN_NEWS_SYNC` 預設關 |
| **LLM** | L9 Memo 敘事 | `investment_memo.py` | `research_memos` / `reports/*.md` | ② 後段 opt | 🔶 `RUN_MEMO=0` 預設 |

**儲存**：一律 upsert 至 **`data/stocks.db`**；備份建議本機 Time Machine 或複製 `data/` 目錄。

**API 政策（與 PRD 一致）**：Score / Intent / 行為引擎 **禁止** 分析時逐檔重打 TEJ；新聞 **禁止** 全市場 2000 檔每日拉。

---

## 核心模組說明

| 模組 | 功能 | 對應檔案 | 狀態 |
| --- | --- | --- | --- |
| **Data Engine** | 多源抓取、清洗、upsert、Skip 語意、log | `daily_sync.sh`、`weekly_sync.sh`、`stock_db.py`、各 `sync_*` | ✅ |
| **Behavior Engine** | L1–L6、共識、輪動、意圖、註解 | `signal_engine.py`、`position_intent.py`、`comment_engine.py`、`holdings_research.py` | ✅ |
| **Universe Engine** | Money ∥ Event 研究池 | `research_universe.py`、`event_ranking.py` | ✅ P1 |
| **Score Engine** | 五維 Investment Score + Rule 觀察名單 | `score_engine.py` | ✅ P2 |
| **Catalyst Engine** | L7 結構化事件 | `catalyst_engine.py`、`sync_catalyst_news.py` | 🔶 P4（opt） |
| **Expectation Engine** | L8.5 預期差、加速度 | `expectation_engine.py` | ✅ P3 |
| **Factor Engine**（可選） | 傳統多因子 | — | 📋 長期 |
| **Ranking Engine**（可選） | 因子橫截面排名 | — | 📋 長期 |
| **Portfolio Engine** | 權重、再平衡版本 | `portfolio_engine.py`、`pm_watchlist.py` | ✅ |
| **Flow Attribution** | §0 ETF Flow 事後歸因 | `flow_attribution.py`、`sync_flow_events.py` | ✅ v0.3 |
| **Risk Engine** | 集中度、科技 ADR gate、beta | `tech_risk` 規則 + Score Risk 子分* | 🔶 部分 |
| **Execution Engine** | 自動下單、成交 | — | ❌ Out of Scope |
| **Analytics Engine** | 績效、回撤、換手 | — | ❌ |

---

## 資料表分層規範

> **實體庫**：`data/stocks.db`（SQLite，本地唯一真相來源）。  
> 下表 **粗體** 為已存在；*斜體* 為 PRD 規劃。  
> 概念名 `raw_*` / `factor_*` 保留，便於日後若擴表仍維持分層語意。

### Raw Layer（原始資料層 · Ingest 寫入）

| 概念表（原架構） | 現行實作表 | 主要來源 | 狀態 |
|------------------|------------|----------|------|
| raw_price_daily | **`daily_bars`** | TEJ、Yahoo | ✅ |
| raw_institutional_trades（ETF） | **`etf_daily_signal_snapshot`** | FinMind | 🔶 opt |
| raw_institutional_trades（成分股） | **`stock_institutional_daily`** | FinMind | ✅ |
| —（ETF 持股專用） | **`etf_holdings`**、**`etf_holdings_meta`** | 官網/EZMoney | ✅ |
| raw_fundamentals | **`stock_fundamental`**、*`stock_consensus`* | FinMind/TEJ | ✅ / 📋 |
| raw_market_meta / 風險 | **`tech_risk_daily_snapshot`**、**`stock_beta`** | Yahoo、FinMind | ✅ / 週 |
| raw_price_daily（成分股） | **`stock_daily_bars`** | FinMind | ✅ |
| update_logs | **`logs/daily_sync_*`**、**`logs/weekly_sync_*`** | 本機檔案 | ✅ |
| raw_margin_balance | — | — | 📋 未規劃 |
| intraday | **`intraday_1m_bars`**、**`intraday_signals`** | 獨立 | 🔶 非主鏈 |

### Signal Layer（行為訊號層 · 讀 Raw、無 API）

| 表 / 產物 | 說明 | 狀態 |
|-----------|------|------|
| —（報告為主） | L1–L6、`position_intent`、log `--intent` | ✅ |
| **`flow_events`** | ETF Flow 事件快照（防 logic drift） | ✅ v0.3 |
| *`catalyst_events`* | L7 Why | 🔶 opt |

### Score Layer（評分研究層 · 讀 DB）

| 概念表（原 factor_*） | 現行規劃 | 狀態 |
|----------------------|----------|------|
| factor_scores / composite_scores | **`investment_scores`**（五維 + 總分 + watchlist） | ✅ P2 |
| factor_rankings | Universe 排名 + Rule A/B/C | ✅ P1–P2 |
| factor_snapshots | `as_of_date` + 規則版本 metadata | ✅ P2 |

### Portfolio Layer（投組管理層）

- **`pm_watchlist`**、**`portfolio_weights`** — ✅；*`portfolio_targets`*、*`rebalance_versions`* — 📋 未建

### Execution Layer（交易執行層）

- *`execution_orders`*、*`execution_fills`*、*`execution_logs`*、*`risk_snapshots`*、*`kill_switch_logs`* — ❌ Out of Scope

### Analytics Layer（績效分析層）

- *`analytics_performance`*、*`analytics_execution`*、*`analytics_risk`*、*`daily_strategy_reports`*、*`reconciliation_logs`* — ❌ 未建

### Memo Layer（敘事層 · PRD 增補）

- *`research_memos`* 或 `reports/YYYYMMDD_memo.md` — 📋 P4

---

## 與 PRD 四層對照

| PRD 層 | 本架構 Layer | 完成度 |
|--------|--------------|--------|
| Layer A · Ingest | **Data Layer**（①②③） | **~75%**（ETF 主鏈 + 成分股 + 基本面） |
| Layer B · L1–L6 + Intent + `flow_events` | **Research**（`compute_etf_behavior`） | ✅ P0 |
| Layer C · 雙引擎 + Score + Rule | **Research** + **Portfolio 前段** | ✅ P1–P2 |
| Layer D · Memo / Catalyst | **Research**（`generate_memo` / L7） | 🔶 opt（預設關） |
| — | **Analytics ④** | ✅ v0.3 Signal Review |
| — | **Execution** | ❌ 刻意不做 |

---

## 五大市場手法 · 分層防線（摘要）

| 手法 | 主要防線層 |
|------|------------|
| 洗盤、當沖 | **Execution**（人工紀律）+ Data 不用 tick 當持股訊號 |
| 誘多誘空、邊拉邊出 | **Research**（Rule、L7、多日法人*）+ 禁止 L1/單日 K 主導 |
| 現貨+期貨 | **Data**（`tech_risk`）→ **Research** Risk 子分；非個股洗盤判斷 |
| ETF 慢變量 | **Data** ② 收盤後 snapshot + **Behavior Engine** |

---

## 相關文件

- [PRD.md](./PRD.md) — 決策引擎規格、§22 改造清單  
- [daily-operations.md](./daily-operations.md) — 方案 C 速查  

---

*最後更新：2026-06 · 對齊 PRD v0.3 · 儲存：本地 SQLite only*
