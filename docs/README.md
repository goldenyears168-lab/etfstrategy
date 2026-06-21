# 文件索引

| 文件 | 內容 |
|------|------|
| **[terminology.md](./terminology.md)** | **術語規範 SSOT**（中英 · 分層 · 文獻 lineage） |
| **[architecture.md](./architecture.md)** | **現行架構 · Facts / Regime / Strategy 分層** |
| [src-map.md](./src-map.md) | **`src/` 模組分層 · 主線 vs research** |
| [daily-operations.md](./daily-operations.md) | 每日 SOP |
| [00981a-copytrade-research-methodology.md](./00981a-copytrade-research-methodology.md) | 00981A 跟單研究 |
| [evaluation-contract.md](./evaluation-contract.md) | Backtest spec · per-track JSON |
| [PRD.md](./PRD.md) | **現行產品範圍**（living doc） |

---

## 收盤主線

- **Facts** — `reports/daily/etf-daily/daily_brief.md`（`layer: facts`）
- **Regime** — `reports/daily/regime/daily_brief.md`（`layer: regime`）

## 手動研究 ID

**`00981a-l1h9`（L1H9）** · `rrg-mono-hold7` · VCP launchd

用語：**[terminology.md](./terminology.md)** · 清障：**[terminology-audit.md](./terminology-audit.md)**

---

## 已移除

- `00981a-v9-hybrid` / `etf_behavior_predict` 全棧
- `qlib-tw-factor` 全棧
- Swing 軌（`breakout_trade_planner`、`morning-regime` gate）
- E0 執行層（`portfolio_engine`、`order_intents`、`execution_eval_runs`）
