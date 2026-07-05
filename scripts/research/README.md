# scripts/research — 回測與探索研究

**不在** `daily_sync.sh` 收盤鏈內。需要時手動執行，或使用 `scripts/run_research_sweep.py`。

**SSOT**

| 檔案 | 用途 |
|------|------|
| [`config/research.yaml`](../config/research.yaml) | Research topic · graduation gates |
| [`config/pipeline_scripts.yaml`](../config/pipeline_scripts.yaml) | daily_sync / launchd 腳本 |
| [`docs/research-script-inventory.md`](../docs/research-script-inventory.md) | 腳本盤點表 |

已刪除（2026-06）：S04 因子 sweep · FinPilot/tw_stocker 對照 · tanish momentum · breadth impulse validation · graduated 一次性 backtest wrapper · `reports/research/` 舊產物。

回測引擎仍保留於 `src/research/backtest/`（`copytrade_backtest` · `chunge_funnel_backtest` · `rrg_mono_backtest` 等）。

## Copytrade / 00981A

| 檔案 | topic |
|------|-------|
| `run_00981a_holdings_rrg_audit.py` | copytrade-rrg-audit |

## RRG

| 檔案 | topic |
|------|-------|
| `run_rrg_mono_score_swap_c.py` | rrg-mono-score-swap-c（採納後 champion 重跑） |
| `run_rrg_mono_intraday_*` | rrg-mono-hold3-tactical |
| `run_rrg_lens_score_swap.py` | rrg-lens-score-swap |

## Pipeline（非本目錄 SSOT）

daily_sync / launchd 腳本見 [`config/pipeline_scripts.yaml`](../config/pipeline_scripts.yaml) · [`scripts/ops/README.md`](../ops/README.md)
