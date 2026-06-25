# scripts/research — 回測與探索研究

**不在** `daily_sync.sh` 收盤鏈內。需要時手動執行。

**SSOT**

| 檔案 | 用途 |
|------|------|
| [`config/research.yaml`](../config/research.yaml) | Research topic · graduation gates |
| [`config/pipeline_scripts.yaml`](../config/pipeline_scripts.yaml) | daily_sync / launchd 腳本（非 research） |
| [`docs/research-script-inventory.md`](../docs/research-script-inventory.md) | 腳本盤點表 |

## Copytrade / 00981A

| 檔案 | topic |
|------|-------|
| `run_00981a_copytrade_backtest.py` | copytrade-hypothesis-matrix |
| `run_00981a_holdings_rrg_audit.py` | copytrade-rrg-audit |

## RRG

| 檔案 | topic |
|------|-------|
| `run_rrg_mono_breadth_backtest.py` | rrg-mono-breadth-study |
| `run_rrg_mono_score_swap_c.py` + `run_c18*.py` | rrg-mono-score-swap-c |
| `run_rrg_mono_intraday_*` | rrg-mono-hold3-tactical |
| `run_rrg_lens_score_swap.py` | rrg-lens-score-swap |

## VCP

| 檔案 | topic |
|------|-------|
| `run_chunge_funnel_sweep.py` · `run_chunge_funnel_backtest.py` | chunge-funnel-sweep |

VCP 文獻校準已封存 → `scripts/research/archive/` · `src/research/archive/vcp_calibration/`

## 因子 / 對照

| 檔案 | topic |
|------|-------|
| `run_s04_*.py` · `run_factor_validation.py` | factor-validation-s04 |
| `run_finpilot_vs_l1_compare.py` · `run_tw_stocker_vs_l1_compare.py` | external-strategy-compare |
| `run_breadth_impulse_validation.py` | breadth-impulse-validation |
| `run_tanish_momentum_backtest.py` | tanish-momentum-breadth |
| `run_broad_momentum_tv_backtest.py` | broad-momentum-sepa |

## Pipeline（非本目錄 SSOT）

daily_sync / launchd 腳本見 [`config/pipeline_scripts.yaml`](../config/pipeline_scripts.yaml) · [`scripts/ops/README.md`](../ops/README.md)
