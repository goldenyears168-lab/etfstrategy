# PRD · ETF 持股研究 · Research OS

| 欄位 | 內容 |
|------|------|
| 版本 | 1.0（現行） |
| 狀態 | **Living doc** — 以程式與 `config/` 為準 |
| 詳細架構 | [architecture.md](./architecture.md) · [daily-operations.md](./daily-operations.md) |

> 免責：產出僅供個人研究，不構成投資建議；下單層僅本機 infra（不進公開網站、非投資建議）。

---

## 1. 產品定位

台股 **ETF 持股變化** 為核心的 **Multi-Research OS**：

- 本地 **SQLite**（`data/stocks.db`）+ 排程 ingest
- **多條 alpha 軌並列**（無 ensemble 加權）
- 各軌 **backtest spec** 在 `config/strategy.yaml` · 探索主題在 `config/research.yaml`
- **下單層**（`config/order.yaml` · `src/order/`）本機送單 infra；**不**進公開網站、**非**產品日報層

---

## 2. 資料層

| 來源 | 模組 | 排程 |
|------|------|------|
| ETF 持股 | `sync_etf_holdings.py` | daily / weekly |
| 成分日線 | `sync_stock_market_daily.py` · TEJ / FinMind | daily |
| 籌碼 | `sync_stock_chip_daily.py` | daily（可關） |
| Tech risk | `sync_tech_risk_context.py` | daily |
| 基本面 | `sync_fundamentals.py` | weekly |

**原則**：ingest 寫 DB；研究／評分 **預設只讀 DB**（`daily_sync.sh` 編排）。

---

## 3. 收盤產物

設定：`config/pipelines/daily_close.yaml` · 產出：`reports/daily/etf-daily/daily_brief.md`

| Strategy ID | 問題 | 模組 |
|-------------|------|------|
| `etf-daily` | 各 ETF 持股變化 | `etf_daily_report` |
| `regime-daily` | Regime 四格雷達 | `regime_daily_brief` |

**手動研究**（非 daily close）：**`00981a-l1h9`（L1H9）** · `rrg-mono-hold7` · VCP launchd

**不進 digest**：`minervini-sepa-basket`（回測）、`shared-analytics`。Zweig/Deemer 廣度推力僅 **Regime 診斷**（`config/regime.yaml`）。

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

## 6. 每日排程

| 時間 | 工作 |
|------|------|
| 13:00 | VCP funnel brief · RRG mono 盤中 watch |
| 16:30 | `daily_sync.sh`（ingest → ETF 日報 → Regime 日報） |
| 16:40 | RRG mono 槽位確認 |
| 週日 20:00 | `weekly_sync.sh` 補庫 |

---

## 7. 報告目錄

| 路徑 | 內容 |
|------|------|
| `reports/daily/` | 排程產物 · digest · `{strategy_id}/` |
| `reports/research/` | 回測 · 廣度 HTML · copytrade 矩陣 |
| `reports/samples/` | 可提交範例（版控） |

索引：[reports/README.md](../reports/README.md) · 路徑常數：`src/report_paths.py`。

---

## 8. Strategy · backtest spec

- **SSOT**：`config/strategy.yaml` → `strategies.*.backtest`（採納規格 · JSON 路徑 · metrics）
- **探索**：`config/research.yaml` → `topics.*`（sweep · 矩陣 · 採納前）
- **產出**：`reports/research/` 下各軌 backtest JSON / HTML · 手動 `run_*_backtest.py`
- **無** cross-track league table · **無** daily 審計排程

---

## 9. 設定地圖

| 檔案 | 管什麼 |
|------|--------|
| `config/strategies.yaml` | 產物 registry · env 開關 · 報告 publish |
| `config/regime.yaml` | Regime 層 · 四軸 |
| `config/research.yaml` | Research 層 · 探索主題 · graduation |
| `config/strategy.yaml` | Strategy 層 · 採納規格 · backtest · schedule |
| `config/investment_policy.example.yaml` | 研究 IPS（paper sim · 複製到 `data/`） |
| `docs/terminology.md` | **術語規範 SSOT** |
| `docs/terminology-audit.md` | 清障清單 · grep 追蹤 |
| `.env` | API token · `RUN_*` 開關 |

---

## 10. 已移除（勿再引用）

| 項目 | 說明 |
|------|------|
| `00981a-v9-hybrid` / behavior stack | 見 [00981a-retired-research.md](./00981a-retired-research.md) |
| `qlib-tw-factor` | 已自 repo 移除 |
| E0 下單 / order_intents / execution_eval | 舊 E0 執行軌退役（現行下單層見 `src/order/`） |
| Swing 軌 / `portfolio_engine` / `portfolio_weights` | 突破計畫與 E0 部位建議已移除 |
| `exposure_coach_tw` / Exposure overlay | Market posture 合成與 live gate 已移除 |
| Evaluation layer · `track_evaluation` · `evaluation_contract` · `signal_review` | 跨軌 ex-post 審計已移除；backtest spec 併入 strategy.yaml |
| LLM Memo / 催化引擎 / ensemble digest | 不在現行 scope |

Copytrade 方法論保留：[00981a-copytrade-research-methodology.md](./00981a-copytrade-research-methodology.md)。

---

## 11. 非目標（Out of Scope）

- 公開網站送單、匿名前端暴露券商憑證
- Ensemble 加權合併多軌訊號
- 即時 Level-2（僅 FinMind tick 盤中快照）

（本機下單層 · 富邦 Neo intent 送單見 `config/order.yaml`；策略腳本只寫 JSON，不 import `order`。）

---

## 12. 成功標準（現行）

1. 每交易日 **16:30 後** 可讀 Facts（`etf-daily`）與 Regime（`regime-daily`）daily brief
2. Strategy 採納規格 **獨立** 回測／launchd；**不** ensemble 合成指令
3. 增刪採納策略：**只改** `config/strategy.yaml` + `strategies.yaml` 對齊；探索主題改 `config/research.yaml`
