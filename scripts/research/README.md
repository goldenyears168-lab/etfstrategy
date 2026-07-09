# scripts/research — 回測與探索研究

**不在** `daily_sync.sh` 收盤主線內（pipeline 腳本見 [`config/pipeline_scripts.yaml`](../config/pipeline_scripts.yaml)）。

**Phase C（2026-07-09）**：archived research 的 improving watch / market probe 預設關 · buy-radar 不算 Improving 池。

## 目錄

| 路徑 | 用途 |
|------|------|
| [`archive/`](archive/) | Phase B · `research.yaml` **archived** topic 的 runner（77 支） |
| `scripts/run_*.py`（根目錄） | **active** / **graduated** 仍會跑的 runner |

## SSOT

| 檔案 | 用途 |
|------|------|
| [`config/research.yaml`](../config/research.yaml) | Research topic · graduation · Phase A 收斂 |
| [`config/pipeline_scripts.yaml`](../config/pipeline_scripts.yaml) | daily_sync / launchd |
| [`docs/research-script-inventory.md`](../docs/research-script-inventory.md) | 腳本盤點 |

## Active research（2026-07-09 · 6 topics）

| Topic | 主要 runner |
|-------|-------------|
| `c18acc-abc-dual-sleeve` | `run_c18acc_abc_dual_sleeve_phase*.py` |
| `c18acc-snapshot-1300` | `run_c18acc_snapshot_1300_sweep.py` |
| `c18acc-extension-radar` | `run_c18acc_extension_screen.py` · phase3b |
| `copytrade-rrg-audit` | `run_00981a_holdings_rrg_audit.py` |
| `finmind-low-rebound-*` · `finmind-low-ps-*` | `run_finmind_*`（待實作/backlog） |

## Graduated · 仍可能重跑 champion

| 檔案 | 說明 |
|------|------|
| `run_rrg_mono_score_swap_c.py` | C18acc 母題 |
| `run_triple_wma_pullback_sweep.py` | ABC v3 母題 |
| `run_abc_v3_f1_pipeline.py` | ABC pipeline |
| `run_00981a_copytrade_backtest.py` | L1H9 |

回測引擎：`src/research/backtest/`（勿刪）。

## Pipeline（非本目錄）

`run_rrg_improving_watch_daily.py` · `run_buy_signal_radar.py` 等見 [`config/pipeline_scripts.yaml`](../config/pipeline_scripts.yaml)。
