# scripts/research — 回測與探索研究

**不在** `daily_sync.sh` 收盤主線內（pipeline 腳本見 [`config/pipeline_scripts.yaml`](../config/pipeline_scripts.yaml)）。

**Phase D（2026-07-09）**：收斂至 0 active research topic 後，`abc-v3-f1-entry-structure` 為首個新開 active topic；其餘非 pipeline 的 `run_*.py` 全在 [`archive/`](archive/)。

## 目錄

| 路徑 | 用途 |
|------|------|
| `run_abc_v3_f1_entry_structure_sweep.py` | Active · ABC v3+F1 entry structure sweep |
| `run_abc_v3_f1_entry_gate_tp_sweep.py` | Active · entry gate × TP-only 聯合 sweep（無槽位限制） |
| `run_abc_v3_f1_entry_multifactor_tp_sweep.py` | Active · gap band × 多因子 overlay × 固定 TP-only |
| `run_abc_v3_f1_entry_factor_scan.py` | Active · entry 因子相關性掃描（raw/z/percentile/ratio） |
| `run_abc_v3_f1_w3_improving_extended_window.py` | Active · RP-1 · W3 Improving 延伸窗口驗證（H-ENTRY-IMP-1） |
| [`archive/`](archive/) | archived / graduated 重跑 runner（手動） |
| `scripts/run_*.py`（根目錄） | **pipeline / strategy daily** 僅 25 支 |

## SSOT

| 檔案 | 用途 |
|------|------|
| [`config/research.yaml`](../config/research.yaml) | Research topic · graduation · Phase A–D 收斂 |
| [`config/pipeline_scripts.yaml`](../config/pipeline_scripts.yaml) | daily_sync / launchd |
| [`docs/research-script-inventory.md`](../docs/research-script-inventory.md) | 腳本盤點 |

## Active research

**1 topic**（`abc-v3-f1-entry-structure` · MV gap sweet-band 進場 + W3 Improving 延伸驗證 RP-1，parent `abc-v3-f1-pullback`）。新探索請先加 topic 再寫 runner；重跑舊 sweep 見 `archive/` + yaml `run_scripts`。

## Graduated · 重跑 champion

| 檔案 | 說明 |
|------|------|
| `archive/run_rrg_mono_score_swap_c.py` | C18acc 母題 |
| `archive/run_triple_wma_pullback_sweep.py` | ABC v3 母題 |
| `archive/run_abc_v3_f1_pipeline.py` | ABC pipeline |

回測引擎：`src/research/backtest/`（production **48** 模組）· sweep 見 `archive/`（**86**）。

## Pipeline（非本目錄）

`run_rrg_improving_watch_daily.py` · `run_buy_signal_radar.py` · `run_c18acc_extension_screen.py` 等見 [`config/pipeline_scripts.yaml`](../config/pipeline_scripts.yaml)。
