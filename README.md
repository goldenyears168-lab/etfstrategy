# ETF 持股研究 · 投資決策引擎

本地 **SQLite**（`data/stocks.db`）+ 方案 C 三段排程。文件見 [docs/README.md](docs/README.md)、[docs/PRD.md](docs/PRD.md)。

## 目錄

| 路徑 | 內容 |
|------|------|
| `src/` | Python：同步、行為訊號、DB（`stock_db.py`） |
| `scripts/` | `daily_sync.sh`、`weekly_sync.sh`、`.command` 入口 |
| `docs/` | PRD、架構總表、營運速查 |
| `data/` | `stocks.db`（gitignore） |
| `logs/` | 同步 log（gitignore） |
| `tests/` | 單元測試 |
| `.cursor/skills/` | Agent 工作流（同步、排程、Intent） |
| `archive/` | 已封存文件（舊雲端方案、素材等） |

## 日常操作

```bash
cd "/Users/jackm4/Documents/ETF/股票研究"
# 或雙擊 scripts/ 內 .command
scripts/ETF早盤風險哨.command      # ① 08:30
scripts/ETF收盤持股雷達.command    # ② 16:30
scripts/ETF週日深度補庫.command    # ③ 週日
```

手動單支腳本（需在專案根目錄）：

```bash
export PYTHONPATH=src
.venv/bin/python src/sync_etf_holdings.py --etf-codes 00981A --changes --intent
```

## 測試

```bash
export PYTHONPATH=src
.venv/bin/python -m unittest tests.test_signal_engine -v
```
