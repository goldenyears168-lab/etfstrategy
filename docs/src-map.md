# src/ 模組分層對照

> **Single source of truth** for `src/` layering.  
> Import rule：**L3 daily pipeline 不得 import L5 research backtest**（`copytrade.signals` 例外：L4 訊號定義，非回測）。

## 分層定義

| 層 | 目錄（目標） | 職責 |
|----|-------------|------|
| **L0** Platform | `stock_db`, `project_config`, `report_paths`, `finmind_client`, `notify_email`, `project_dotenv`, `research_config`, `regime_config` | DB、設定、路徑、registry |
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
| **`00981a-l1h9`** | **`copytrade/signals`** · `copytrade_backtest`（L1H9） | 手動回測 |
| `rrg-mono-hold7` | `rrg_mono_daily_brief`, `rrg_rotation` | launchd 16:40 |
| `vcp-pivot-gate` / `vcp-coil-close` | `vcp_funnel_screen`, `chunge_funnel_screen` | launchd 13:00 |

**已退役 daily 鏈**：`p6-tier-flow`（`score_engine` → `pm_watchlist` · `RUN_SCORE_ENGINE=0`）

---

## L5 Research（`src/research/`）

| 路徑 | 模組 |
|------|------|
| `research/backtest/` | copytrade、RRG、VCP funnel、`slot_backtest_summary` |
| `research/archive/` | VCP 校準、一次性 migration |
| `src/{name}.py` shim | → `research.backtest.*`（flat import 相容） |

## Platform

| 路徑 | 模組 |
|------|------|
| `stock_db/` | `_core` · `copytrade` DDL · `util` |
| `copytrade/signals.py` | 主線 copytrade 訊號 |
| `analytics/bench.py` | 基準報酬 · 超額檢定 |
| `research_config.py` | Load `config/research.yaml`（探索主題） |
| `strategy_config.py` | Load `config/strategy.yaml`（採納規格） |

其餘 daily 模組仍在 `src/` 頂層。

---

## 已封存（不在主線）

`research/archive/` 含 VCP 校準整包、etf flow 因子檢定、一次性 migration。  
Ops 工具：`backfill_market_data` · `etfedge_*`

**已移除**：`pipeline_evening`, `research_os`, `evening_digest`, `track_evaluation`, `signal_review`（頂層模組）

---

## 依賴規則（enforce 目標）

1. `copytrade.signals` → 僅 L0 + `holdings_research` 語意
2. L3 daily pipeline → **不** import `research.backtest.*`
3. L4 launchd briefs → import `analytics.bench`，不 import `copytrade_backtest`
4. Backtest JSON → `slot_backtest_summary` + 各 `run_*_backtest.py`

---

## 相關文件

- [architecture.md](./architecture.md)
- [PRD.md](./PRD.md)
- [evaluation-contract.md](./evaluation-contract.md)（backtest spec）
