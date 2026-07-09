# research/backtest/archive — 非 production import 鏈

**2026-07-09 Phase D+** · sweep / 假說 / 已封存 research 的 backtest 引擎。

## 邊界

| 路徑 | 用途 |
|------|------|
| `src/research/backtest/*.py`（48 模組） | **production import 鏈** · daily pipeline · strategy screen |
| `src/research/backtest/archive/*.py`（86 模組） | 手動重跑 · archived runner 專用 |

Production `src/` **不得** import `research.backtest.archive.*`（L3 規則延伸）。

## Production 核心（摘要）

`finpilot_local_backtest` · `rrg_mono_score_swap_c` · `rrg_mono_intraday_ab` ·
`rrg_mono_backtest` · `rrg_mono_swap_exit_b` · `triple_wma_pullback_sweep` ·
`w3_rv_hl_winner_profile` · `c18acc_extension_exit` · `dual_wma_signal_backtest` · …

完整 closure 由 `config/pipeline_scripts.yaml` + `src/`（不含本目錄）transitive import 決定。

## 重跑 archived sweep

```bash
cd "<project-root>"
PYTHONPATH=src .venv/bin/python scripts/research/archive/run_abc_v3_slot_sweep.py --dry-run
```

Runner 在 `scripts/research/archive/` · 引擎在本目錄。
