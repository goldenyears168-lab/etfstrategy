# Agent brief · 任務導航（省 token 用）

> **Scope**: 給 Cursor / LLM 的「先讀這份再動手」索引。  
> **Non-goals**: 不取代 [architecture.md](./architecture.md)、[src-map.md](./src-map.md)、[terminology.md](./terminology.md)。  
> **用法**: 提 task 時 `@docs/agent-brief.md` + 下方對照表所列 1–3 個檔案。

---

## 邊界（勿違反）

1. **Product layers** ≠ **`src/` L0–L5** — 見 [terminology.md](./terminology.md) §1。
2. **L3 daily pipeline 不得 import L5 `research.backtest.*`**（`copytrade.signals` 例外：L4 訊號定義）。
3. **Research** → `config/research.yaml`；**採納 Strategy** → `config/strategy.yaml` + `config/strategies.yaml`。
4. **Order layer**（`src/order/`）不進策略 import 鏈；只寫 `reports/order/intents/*.json`。
5. 術語：prose 用 `English term（中文術語）`；完整禁語表見 [terminology.md](./terminology.md) §10。

---

## Config SSOT

| 用途 | 檔案 |
|------|------|
| 收盤 DAG | `config/pipelines/daily_close.yaml` |
| 策略 registry · enabled · publish | `config/strategies.yaml` |
| 採納規格 · backtest | `config/strategy.yaml` |
| 探索主題 · sweep | `config/research.yaml`（Phase D · v5 · 0 active） |
| Regime 四軸 | `config/regime.yaml` |
| 下單意圖 | `config/order.yaml` |
| Pipeline / launchd 腳本 registry | `config/pipeline_scripts.yaml` |
| Gate · `RUN_*` | `src/pipeline_gates.py` · `.env` |
| 買入觀測池 | `config/buy_observation.yaml` |

---

## 任務 → 先讀 → 可能改

| 任務 | 先讀 | 可能改 |
|------|------|--------|
| **daily close / 16:30 收盤** | `scripts/daily_sync.sh`, `config/pipelines/daily_close.yaml`, `src/pipeline_gates.py` | `config/strategies.yaml`, 各 `src/*_daily*.py` |
| **Facts · ETF 持股** | `src/etf_daily_report.py`, `config/strategies.yaml` · `etf-daily` | `src/sync_etf_holdings.py` |
| **Regime 四軸日報** | `src/regime_daily_brief.py`, `config/regime.yaml` | `src/regime_snapshot.py`, `src/market_*` |
| **00981A Copytrade · L1H9** | `src/copytrade/signals.py`, `config/strategy.yaml` · `00981a-l1h9` | `src/copytrade_l1h9_daily.py`, `scripts/run_copytrade_l1h9_daily_brief.py` |
| **RRG mono / swap-accel** | `docs/RRG相對輪動圖入門.md`, `src/rrg_mono_daily_brief.py`, `src/rrg_rotation.py` | `scripts/run_rrg_universe_close.py` |
| **RRG Improving lifecycle** | `docs/rrg-improving-lifecycle-research.md`, `scripts/run_rrg_improving_lifecycle_backtest.py`, `scripts/run_rrg_improving_lifecycle_monthly_sweep.py`, `scripts/run_rrg_improving_watch_daily.py` | `src/research/backtest/rrg_improving_lifecycle_*.py`, `src/rrg_improving_watch.py`, `config/buy_observation.yaml` |
| **VCP funnel** | `src/vcp_funnel_specs_daily.py`, `src/vcp_funnel_screen.py` | `scripts/run_vcp_funnel_*.py` |
| **buy / sell signal radar** | `src/strategy_signal_radar.py`, `config/strategy.yaml` · `buy-signal-radar` / `sell-signal-radar` | `scripts/launchd/*-signal-radar.command` |
| **launchd 排錯** | `config/pipeline_scripts.yaml`, `docs/daily-operations.md` | 對應 `scripts/launchd/*.command` |
| **下單 · Fubon** | [order-layer-prd.md](./order-layer-prd.md), `src/order/`, `config/order.yaml`, `scripts/order/` | `reports/order/`（runtime） |
| **回測 · 採納策略** | `config/strategy.yaml`, `docs/evaluation-contract.md` | `src/research/backtest/`（production 48 模組）· sweep 見 `backtest/archive/` |
| **探索 sweep** | `config/research.yaml`, `scripts/run_research_sweep.py` | `src/research/` |
| **FinMind 策略市集回測** | `docs/strategy-marketplace/README.md`, `config/research.yaml` · `topics.finmind-*`（archived backlog） | `scripts/research/archive/run_finmind_*` |
| **Readdy 公開站** | `docs/architecture.md` § Readdy | 前端 · Supabase publish 腳本 |
| **FinMind 取數** | `src/finmind_client.py`, `.cursor/rules/finmind.mdc` | ingest `src/sync_*` |

---

## 常用驗收命令

```bash
# slim 收盤（Facts + Regime only）
SYNC_PROFILE=slim scripts/daily_sync.sh --holdings-report

# registry vs RUN_* 不一致
PYTHONPATH=src .venv/bin/python src/pipeline_gates.py list-mismatches

# 單測（依任務替換路徑）
.venv/bin/pytest tests/ --ignore=tests/research/archive -q   # production
.venv/bin/pytest tests/research/archive/ -q                 # archived backtest
```

---

## 日誌 · 產物（需要時才 @，勿整檔 grep）

| 類型 | 路徑 |
|------|------|
| daily_sync | `logs/daily_sync_YYYYMMDD.log` |
| launchd | `logs/replay/launchd_<job>_YYYYMMDD.log` |
| Facts brief | `reports/daily/etf-daily/daily_brief.md` |
| Regime brief | `reports/daily/regime/daily_brief.md` |
| Order intents | `reports/order/intents/*.json` |

排錯時 **貼 30–50 行 relevant snippet**，或指定行號範圍。

---

## 相關文件

- [README.md](./README.md) — 完整 doc 索引  
- [architecture.md](./architecture.md) — 產品分層  
- [src-map.md](./src-map.md) — `src/` L0–L5  
- [daily-operations.md](./daily-operations.md) — infra SOP · launchd  
