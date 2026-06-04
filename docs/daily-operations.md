# 每日營運速查

> **正文以 [PRD.md](./PRD.md) 為準**（v0.3 · **方案 C**）。本檔僅供快速連結。

## 方案 C：三個排程（預設）

| # | 名稱 | slug | 建議時間 | 入口 |
|---|------|------|----------|------|
| ① | **早盤風險哨** | `morning-risk` | 週一至五 08:30 | `scripts/ETF早盤風險哨.command` |
| ② | **收盤持股雷達** | `evening-holdings` | 週一至五 16:30 | `scripts/ETF收盤持股雷達.command` |
| ③ | **週日深度補庫** | `weekly-deep` | 週日 20:00 | `scripts/ETF週日深度補庫.command` |

| 想查什麼 | PRD 章節 |
|----------|----------|
| 架構圖、Phase 對照 | **§5.2** |
| 每天幾次、log 位置 | §5.3 |
| 資料多久更新 | §5.4 |
| Mac 排程時點、TSM ADR | §5.5、§19 |
| ETF 法人 vs 成分股法人 | §5.6 |
| 指令對照 | **§18** |
| 改造 checklist | **§22** |
| 五層架構、模組現況 | [architecture.md](./architecture.md) |

**入口**：`scripts/ETF早盤風險哨.command` / `ETF收盤持股雷達.command` / `ETF週日深度補庫.command`  
**相容全量**（除錯）：`scripts/ETF每日同步.command` → `daily_sync.sh --quiet`  
Python 在 **`src/`**；`daily_sync.sh` 已設定 `PYTHONPATH`。
