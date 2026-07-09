# scripts/research/archive — Phase B–D 封存 runner

**2026-07-09 Phase D** · graduated / archived topic 的 `run_scripts` · **0 active research**。

- **不在** daily_sync 主線（Phase C 預設關：`RUN_RRG_IMPROVING_WATCH` · probe radar/backfill）。
- 手動重開 improving：`RUN_RRG_IMPROVING_WATCH=1` · `buy_observation.yaml` 池 `enabled: true`。
- **不刪** `src/research/backtest/` · 測試與 ABC/C18acc 模組仍依賴。
- Production 例外：`scripts/run_c18acc_extension_screen.py` 仍在根目錄（launchd）。
- 手動重跑範例：

```bash
cd "<project-root>"
PYTHONPATH=src .venv/bin/python scripts/research/archive/run_triple_wma_pullback_sweep.py --dry-run
```

Pipeline / strategy daily runner 見 `scripts/run_*.py`（25 支）與 `config/pipeline_scripts.yaml`。
