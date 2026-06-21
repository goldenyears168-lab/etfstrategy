# VCP 校準（已封存 · 2026-06）

**不再維護、不在 daily 主線。** 校準產物已凍結：

- `config/vcp_tm_calibrated.yaml` · `config/vcp_tw_cases.yaml`
- `reports/research/vcp_tw_benchmark.md`（若已產出）

Daily VCP 使用 `src/vcp_screen.py` + `src/vcp_tm/` + `src/vcp_nse_port/`。

## 若必須重跑（不建議）

```bash
PYTHONPATH=src python src/research/archive/vcp_calibration/vcp_tw_literature_audit.py --use-db
PYTHONPATH=src python src/research/archive/vcp_calibration/vcp_tw_benchmark.py --use-db
```

Wrapper 腳本（同目錄邏輯）：`scripts/research/archive/run_vcp_*.py`
