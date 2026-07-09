# scripts/research/archive — Phase B 封存 runner

**2026-07-09** · 對應 `config/research.yaml` · `status: archived` topic 的 `run_scripts`。

- **不在** daily_sync 主線（Phase C 預設關：`RUN_RRG_IMPROVING_WATCH` · probe radar/backfill）。
- 手動重開 improving：`RUN_RRG_IMPROVING_WATCH=1` · `buy_observation.yaml` 池 `enabled: true`。
- **不刪** `src/research/backtest/` · 測試與 ABC/C18acc 模組仍依賴。
- 手動重跑範例：

```bash
cd "<project-root>"
PYTHONPATH=src .venv/bin/python scripts/research/archive/run_rrg_lane_discovery_loop.py --dry-run
```

Active research runner 仍在 `scripts/` 根目錄 · 見 `config/research.yaml` · `status: active`。
