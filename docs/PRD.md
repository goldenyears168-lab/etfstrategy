# PRD · 台股量化交易 Research OS

| 欄位 | 內容 |
|------|------|
| 版本 | 2.1 |
| 最後更新 | 2026-07-24 |
| 狀態 | **Living doc** — 以程式與 `config/` 為準 |
| 詳細架構 | [architecture.md](./architecture.md) · [daily-operations.md](./daily-operations.md) · [agent-brief.md](./agent-brief.md) |

> **免責**：產出僅供個人研究，不構成投資建議；下單層僅本機 infra，所有數據與報告皆在本地。

> ⚠️ **2026-07-23 重大變更**：公開站 Readdy 已退役，`stock_research.*` Supabase schema 已清空。現行系統為**本地研究 OS + 下單層（Order layer）**兩部分，外加私人 ops 後台（非公開展示站）。詳見 §7 與 [`archives/PUBLIC_SITE_RETIRED.md`](../archives/PUBLIC_SITE_RETIRED.md)。

---

## 1. 產品定位

台股 **量化交易研究系統**（**Multi-Research OS**），由**兩個核心部分**組成：

1. **本地研究 OS** — SQLite（`data/stocks.db`）+ 排程 ingest + **多條 alpha 軌並列**（無 ensemble 加權），核心是**個股層級**的策略研究：RRG 動能輪動、VCP 型態篩選、Minervini SEPA、00981A 跟單 copytrade 等。各軌 backtest spec 在 `config/strategy.yaml`，探索主題在 `config/research.yaml`。
2. **下單層（Order layer · 本機 infra）** — `config/order.yaml` · `src/order/`，富邦 Neo 本機送單；策略腳本只寫 `reports/order/intents/*.json`，不 import `order`。完整藍圖見 [order-layer-prd.md](./order-layer-prd.md)。Mac mini 常開自動執行。

**產出形式**：
- 📊 本地 markdown 報告（`reports/daily/`）
- 📁 SQLite 數據庫（104 張表，2.4GB）
- 📧 郵件通知（策略訊號、下單確認）
- 🔐 私人 ops 後台（`haoshi-quant-ops.pages.dev`，非公開展示站）

> **專案沿革**：起源自 ETF 持股追蹤（git 初始 commit「Phase 0: 5 ETF daily sync」），現行 `scripts/` 162 支腳本中僅 2 支與 ETF 直接相關，其餘（RRG 48 支、backtest 26 支、VCP 6 支、copytrade 3 支、signal radar 4 支等）皆為個股層級策略研究。**ETF 持股變化現在是資料層的一項訊號來源**（`etf-daily` Facts 層 + `00981a-l1h9` 跟單訊號輸入），**不是**整個系統的核心；核心是 §6 的多軌策略研究。

---

## 2. 資料層（Ingest → `stocks.db`）

| 類別 | 模組 | 排程 |
|------|------|------|
| ETF 持股 | `sync_etf_holdings.py` | daily / weekly |
| ETF 訊號 | `sync_etf_signal.py` | daily |
| 個股日線（TEJ / FinMind） | `sync_stock_market_daily.py` | daily |
| 美股日線 | （`us_daily_bars`，境外對照用） | daily |
| 籌碼（三大法人、融資券） | `sync_stock_chip_daily.py` | daily（可關） |
| 股權分布 | `sync_stock_shareholding_daily.py` | daily |
| 主力籌碼 | `sync_stock_sponsor_daily.py` | daily |
| 期貨法人 | `sync_futures_institutional_daily.py` · `sync_morning_futures.py` | daily |
| 市值 | `sync_stock_market_value_daily.py` | daily |
| 技術指標 | `sync_stock_technical_daily.py` | daily |
| Beta | `sync_stock_beta.py` | weekly |
| 基本面 | `sync_fundamentals.py` | weekly |
| 共同基金持股 | `sync_mutual_fund_holdings.py` | 月報公布觸發 |
| 指數成分 | `sync_benchmark_constituents.py` | weekly |
| Tech risk | `sync_tech_risk_context.py` | daily |
| 資金流事件 | `sync_flow_events.py` · `sync_flow_event_legs.py` | daily |

**原則**：ingest 寫 DB；研究／評分 **預設只讀 DB**（`daily_sync.sh` 編排）。現況：104 張表、`data/stocks.db` 約 2.4GB（詳見 `docs/architecture.md`）。

---

## 3. 收盤產物（Facts / Regime）

設定：`config/pipelines/daily_close.yaml` · 產出：`reports/daily/etf-daily/daily_brief.md`

| Strategy ID | Layer | 問題 | 模組 |
|-------------|-------|------|------|
| `etf-daily` | facts | 各 ETF 持股變化（L1 shares 差分 · 只報事實） | `etf_daily_report` |
| `regime-daily` | regime | Regime 四軸雷達（非 alpha） | `regime_daily_brief` |

**不進 digest**：`shared-analytics`。Zweig/Deemer 廣度推力僅 **Regime 診斷**（`config/regime.yaml`）。

---

## 4. 術語

完整規範：[terminology.md](./terminology.md)

| Canonical term | 用途 |
|----------------|------|
| **Trend posture** | IX0001 · `trend_posture` · Weinstein mapping |
| **Breadth zone** | 200MA 五區間 · 非 live gate |

---

## 5. Regime 日報

- **Strategy ID**：`regime-daily`
- **模組**：`regime_daily_brief.py`
- **產出**：`reports/daily/regime/daily_brief.md`（四軸：Breadth zone · Trend posture · RRG · Stage-2）
- **設定**：`config/regime.yaml`

---

## 6. 策略 registry（`config/strategies.yaml`，現行 10 項）

| Strategy ID | Layer | Kind | 問題 | 排程 |
|-------------|-------|------|------|------|
| `etf-daily` | facts | diagnostic | 各 ETF 持股變化 | 16:30 daily_sync |
| `regime-daily` | regime | diagnostic | Regime 四軸雷達 | 16:30 daily_sync |
| `00981a-l1h9` | strategy | competition | 00981A 新进/加码跟單 · T+1 開 · hold9 | 手動 screen / 回測 |
| `rrg-mono-hold7` | strategy | competition | RRG mono · seg_last · 3-slot hold7 | 16:30 daily_sync |
| `rrg-mono-swap-accel` | strategy | competition | RRG mono swap-accel · C18acc · 四日加速换仓 | 16:30 收盤 + 盤中 5m poll |
| `rrg-mono-swap-accel-extension` | strategy | competition | C18acc extension overlay（legacy 手動） | 手動 |
| `vcp-pivot-gate` | strategy | competition | VCP Pivot Gate · hold20 | 13:00 盤中 + 16:30 收盤 |
| `vcp-coil-close` | strategy | competition | VCP Coil Close（訊號日 close 變體） | 共用 vcp-funnel-specs launchd |
| `minervini-sepa-basket` | strategy | competition | Minervini Trend Template 7/7 等權 basket | 月末 · 16:35 launchd |
| `buy-signal-radar` | strategy | operational | 多池買入 advisory（寄信、不送單） | 09:00–13:20 每 5 分 |
| `sell-signal-radar` | strategy | operational | 宇宙 extension 賣出 advisory（寄信、不送單） | 09:06–13:20 每 5 分 |

**SSOT**：`config/strategy.yaml`（採納規格 · backtest）· `config/strategies.yaml`（registry · enabled · publish）· `config/research.yaml`（探索主題 · sweep · graduation gates，尚未採納）。

`kind: operational` 為寄信 advisory，屬即時篩選但**不**送單、**不**回測採納（跟 `competition` 的獨立 backtest 採納策略性質不同）。

研究中、尚未採納：`docs/strategy-marketplace/`（FinMind 策略市集，5 個候選主題，`config/research.yaml` → `topics.finmind-*`）。

---

## 7. 私人 Ops 後台（haoshi-quant-ops · 已退役公開站功能）

> ⚠️ **RETIRED 2026-07-23**：原公開站 Readdy (`stock_research.*` Supabase schema) 已退役並清空。  
> 現行為**私人運維後台**，僅供個人查看持倉與實時 TA，**非公開展示網站**。

| 項目 | 說明 |
|------|------|
| **新站** | 獨立 repo `haoshi-quant-ops` → `https://haoshi-quant-ops.pages.dev` |
| **性質** | 私人運維後台（非公開展示站） |
| **功能** | Live TA（如 2492 華新科）· 持倉狀態 · 策略袖狀態 |
| **數據來源** | `ops.*` schema（非 `stock_research.*`） |
| **同步方式** | `scripts/ops/` 腳本（非 `supabase_sync`） |

**環境變數（已標記 RETIRED）**：
```bash
RUN_SUPABASE_RESEARCH_SYNC=0  # RETIRED 2026-07-23
RUN_SUPABASE_LENS_SYNC=0
RUN_SUPABASE_SIGNAL_SYNC=0
```

**舊前端封存位置**：`~/Documents/股市資料備份封存_20260723/舊站原始碼/`

**詳細說明**：[`archives/PUBLIC_SITE_RETIRED.md`](../archives/PUBLIC_SITE_RETIRED.md) · [`scripts/ops/README.md`](../scripts/ops/README.md)

---

## 8. 每日排程

> Live 排程狀態 SSOT 是 [config/job_registry.yaml](../config/job_registry.yaml)。下表為設計時刻表；截至 2026-08-02，`buy-signal-radar` 已停用、`sell-signal-radar` 已退役（`status` 見 registry），實機不再於盤中觸發。

| 時間 | 工作 |
|------|------|
| ~~09:00–13:20（每 5 分）~~ **已停用** | `buy-signal-radar` |
| ~~09:06–13:20（每 5 分）~~ **已退役** | `sell-signal-radar` |
| 13:00 | VCP funnel brief（盤中）· RRG mono intraday watch |
| 16:30 | `daily_sync.sh`（ingest → ETF 日報 → Regime 日報 → RRG mono/swap-accel → VCP 收盤 → Lens → Supabase sync） |
| 16:35 | `minervini-sepa-basket`（月末) |
| 16:40 | （已退役，併入 16:30 daily_sync） |
| 週日 20:00 | `weekly_sync.sh` 補庫 |

`SYNC_PROFILE=slim|full` 控制 16:30 收盤跑多少策略軌，見 [daily-operations.md](./daily-operations.md)。

---

## 9. 報告目錄

| 路徑 | 內容 |
|------|------|
| `reports/daily/` | 排程產物 · digest · `{strategy_id}/` |
| `reports/research/` | 回測 · 廣度 HTML · copytrade 矩陣 |
| `reports/samples/` | 可提交範例（版控） |

索引：[reports/README.md](../reports/README.md) · 路徑常數：`src/report_paths.py`。

---

## 10. 設定地圖

| 檔案 | 管什麼 |
|------|--------|
| `config/strategies.yaml` | 產物 registry · env 開關 · 報告 publish |
| `config/regime.yaml` | Regime 層 · 四軸 |
| `config/research.yaml` | Research 層 · 探索主題 · graduation |
| `config/strategy.yaml` | Strategy 層 · 採納規格 · backtest · schedule |
| `config/order.yaml` | 下單層 · 富邦 Neo 帳戶 / intent schema |
| `config/pipeline_scripts.yaml` | Pipeline / launchd 腳本 registry |
| `config/investment_policy.example.yaml` | 研究 IPS（paper sim · 複製到 `data/`） |
| `docs/terminology.md` | **術語規範 SSOT** |
| `.env` | API token · `RUN_*` 開關 |

---

## 11. 已移除（勿再引用）

| 項目 | 退役日期 | 說明 |
|------|---------|------|
| **Readdy 公開站** | 2026-07-23 | 移至獨立 repo `haoshi-quant-ops`（私人後台） |
| **`stock_research.*` schema** | 2026-07-23 | Supabase 已清空，改用 `ops.*` |
| **`RUN_SUPABASE_*_SYNC`** | 2026-07-23 | 環境變數標記為 RETIRED |
| `00981a-v9-hybrid` / behavior stack | — | 見 [00981a-retired-research.md](./00981a-retired-research.md) |
| `qlib-tw-factor` | — | 已自 repo 移除（DB 表 `qlib_tw_factor_scores` 仍留審計） |
| E0 下單 / `order_intents` / `execution_eval` | 2026-07-16 | 舊 E0 執行軌退役（現行下單層見 `src/order/`） |
| **ABC Order** | 2026-07-16 | `abc-v3-f1-*` 下單軌退役（`buy-signal-radar` ABC 觀察軌關閉） |
| Swing 軌 / `portfolio_engine` / `portfolio_weights` | — | 突破計畫與 E0 部位建議已移除 |
| `exposure_coach_tw` / Exposure overlay | — | Market posture 合成與 live gate 已移除 |
| Evaluation layer · `track_evaluation` · `evaluation_contract` · `signal_review` | — | 跨軌 ex-post 審計已移除；backtest spec 併入 `strategy.yaml` |
| LLM Memo / 催化引擎 / ensemble digest | — | 不在現行 scope |

Copytrade 方法論保留：[00981a-copytrade-research-methodology.md](./00981a-copytrade-research-methodology.md)。

---

## 12. 非目標（Out of Scope）

- ~~公開站接受下單~~（公開站已於 2026-07-23 退役）
- Ensemble 加權合併多軌訊號
- 即時 Level-2（僅 FinMind tick 盤中快照）
- 多券商統一接口（僅富邦 Neo）

---

## 13. 成功標準（現行）

1. 每交易日 **16:30 後** 可讀 Facts（`etf-daily`）與 Regime（`regime-daily`）本地 daily brief
2. Strategy 採納規格 **獨立** 回測／launchd；**不** ensemble 合成指令
3. 增刪採納策略：**只改** `config/strategy.yaml` + `strategies.yaml` 對齊；探索主題改 `config/research.yaml`
4. ~~公開站資料不 stale~~（**RETIRED 2026-07-23**：改為私人 ops 後台，無公開站健康檢查需求）
