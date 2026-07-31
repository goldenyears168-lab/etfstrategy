# src/ 模組分層對照

> **Single source of truth** for `src/` layering.  
> Import rule：**L3 daily pipeline 不得 import L5 research backtest**（`copytrade.signals` 例外：L4 訊號定義，非回測）。

## 分層定義

| 層 | 目錄（目標） | 職責 |
|----|-------------|------|
| **L0** Platform | `stock_db`, `project_config`, `report_paths`, `finmind_client`, `notify_email`, `project_dotenv`, `research_config`, `regime_config`, `pipeline_gates`, `strategy_registry` | DB、設定、路徑、registry |
| **L1** Ingest | `sync_*`, `query_stock_prices`, `backfill_market_data`, `etfedge_*` | 寫入 `stocks.db` |
| **L2** Domain | `holdings_research`, `market_*`, `flow_*`, `regime_snapshot`, `analytics/bench`, … | 跨軌共用領域邏輯 |
| **L3** Pipeline | `etf_daily_report`, `regime_daily_brief`, `report_hygiene` | 收盤 daily brief |
| **L4** Tracks | 見下表 | launchd / 手動 daily 產物（非 daily_close 主線） |
| **L5** Research | `research/backtest/*`, `factor_validation` | 手動／排程回測，不進 daily import 鏈 |

---

## L3 收盤主線（`daily_sync.sh` · `config/pipelines/daily_close.yaml`）

```text
query_stock_prices → sync_etf_holdings → etf_daily_report → regime_daily_brief
  → reports/daily/etf-daily/daily_brief.md
  → reports/daily/regime/daily_brief.md
```

| Strategy ID | 模組 |
|-------------|------|
| `etf-daily` | `etf_daily_report` |
| `regime-daily` | `regime_daily_brief` |

---

## L4 研究軌（launchd / 手動 · 非 daily_close）

| Track ID | 模組 | 排程 |
|----------|------|------|
| **`00981a-l1h9`** | **`copytrade/signals`** · `copytrade_backtest`（L1H9） | 手動回測 · 16:30 screen（registry + `pipeline_gates`） |
| `rrg-mono-hold7` | `rrg_mono_daily_brief`, `rrg_rotation` | **16:30 daily_sync**（原 16:40 launchd 已退役） |
| `vcp-pivot-gate` / `vcp-coil-close` | `vcp_funnel_screen`, `chunge_funnel_screen` | 13:00 launchd 盤中 · **16:30** close（`RUN_VCP_FUNNEL_CLOSE`） |

**daily_sync gate**：`src/pipeline_gates.py` — `strategies.yaml` · `enabled` 優先於 `RUN_*`。

**已退役 daily 鏈**：`p6-tier-flow`（`score_engine` → `pm_watchlist` · `RUN_SCORE_ENGINE=0`）

### 績效回填（非 daily_close 主線・選用）

`strategy_performance_yearly.py`（`RUN_STRATEGY_PERF_SYNC`，slim profile 預設關）── 重算已採納策略的年度績效（Sharpe／CAGR／win rate）寫入 SQLite + Supabase。**明確允許** import L5 `research.backtest.*`（`chunge_funnel_backtest`、`copytrade_backtest`、`rrg_mono_backtest`、`rrg_mono_score_swap_c`、`finpilot_local_backtest`）── 這是規則 3 的例外：目的就是「用回測引擎算出歷史績效數字」，不是產生每日訊號，且不在 `daily_close` 主線（`etf-daily`／`regime-daily`）或關鍵 L4 track screen 的 import 鏈上。

---

## L5 Research（`src/research/`）

| 路徑 | 模組 |
|------|------|
| `research/backtest/` | copytrade、RRG、VCP funnel、`slot_backtest_summary` |
| `research/backtest/` | 採納策略回測引擎 · unified 比較層 |
| `src/{name}.py` shim | → `research.backtest.*`（flat import 相容） |

## Platform

| 路徑 | 模組 |
|------|------|
| `stock_db/` | `_core` · `copytrade` DDL · `util` |
| `copytrade/signals.py` | 主線 copytrade 訊號 |
| `analytics/bench.py` | 基準報酬 · 超額檢定 |
| `research_config.py` | Load `config/research.yaml`（探索主題） |
| `strategy_config.py` | Load `config/strategy.yaml`（採納規格） |
| `order/config.py` | Load `config/order.yaml`（下單層） |

其餘 daily 模組仍在 `src/` 頂層。

---

## 下單層（`src/order/` · 非 L0–L5 daily 鏈）

| 路徑 | 模組 |
|------|------|
| `order/` | `intent` · `config` · `fubon_session` · `fubon_account` · `fubon_orders` |
| `scripts/order/` | `submit_intents.py` · `fubon_login_test.py` |
| `scripts/order/chase_scheduled.py` | 開盤窗追價（每分鐘 · 限價=賣一 · 最多 5 輪）· **已退役、不裝 launchd**（2026-07-26 撤 mini；`install-order-launchd.sh` 每次執行會自動卸載，見 `LEGACY_LABELS`） |
| `scripts/launchd/order-chase-open.command` | 程式碼仍在，僅供需要時手動 bootstrap；非日常排程 |
| `reports/order/` | intent JSON · 帳戶 snapshot（執行時寫入） |

策略 / research 腳本 **勿** import `order`；只寫 `reports/order/intents/*.json`（schema `order-intent-v1`）。

---

## 已封存（不在主線）

`research/backtest/` 為回測引擎；探索 sweep 用 `run_research_sweep.py` + `sweep_runner`。  
Ops 工具：`backfill_market_data` · `etfedge_*`

**已移除**：`pipeline_evening`, `research_os`, `evening_digest`, `track_evaluation`, `signal_review`（頂層模組）

---

## 依賴規則（enforce 目標）

> ⚠️ **2026-08-01 健檢**：規則3目前**未被強制執行**——約19-20支L4 launchd daily-brief/screen模組實際上都有import `research.backtest.*`（例如 `rrg_mono_daily_brief.py`、`rrg_mono_swap_accel_screen.py`、`c18acc_extension_screen.py`、`vcp_funnel_specs_daily.py`、`minervini_sepa_daily.py`、`regime_snapshot.py`、`buy_observation.py` 等），多半是因為`research/backtest/finpilot_local_backtest.py`的`load_price_panels`（純DB查詢轉wide-DataFrame工具，本身無回測邏輯）被廣泛當共用helper引用。規則本身仍是合理目標，但現況是文件跟程式碼不一致，尚未動手把`load_price_panels`類的共用helper搬出`research/backtest/`。

1. `copytrade.signals` → 僅 L0 + `holdings_research` 語意
2. L3 daily pipeline → **不** import `research.backtest.*`
3. L4 launchd briefs（每日訊號／screen 產出）→ import `analytics.bench`，不 import `copytrade_backtest`；例外見上「績效回填」（非 screen，選用、算歷史績效用）
4. Backtest JSON → `slot_backtest_summary` + 各 `run_*_backtest.py`

---

## 相關文件

- [architecture.md](./architecture.md)
- [PRD.md](./PRD.md)
- [evaluation-contract.md](./evaluation-contract.md)（backtest spec）
