# tests/research/archive

**2026-07-09** · 只測 `research.backtest.archive.*` 的單元測試（56 支）。

Production / 混合 import 的測試留在 `tests/` 根目錄。

```bash
# 只跑 archived backtest 測試
.venv/bin/pytest tests/research/archive/ -q

# 跑 production 主測（排除 archive）
.venv/bin/pytest tests/ --ignore=tests/research/archive -q
```

`pyproject.toml` · `[tool.pytest.ini_options] pythonpath = ["src"]` · 勿在 `tests/research/` 放 `__init__.py`（會遮蔽 `src/research`）。
