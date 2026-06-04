---
name: scheme-c-local-ops
description: >-
  Run ETF research local schedules (Scheme C): morning-risk, evening-holdings,
  weekly-deep via scripts/*.command and daily_sync.sh. Use when setting launchd,
  debugging logs, or choosing which sync profile to run on Mac.
disable-model-invocation: true
---

# 方案 C · 本機排程

## 三支排程

| # | 名稱 | slug | 時間 | 入口 |
|---|------|------|------|------|
| ① | 早盤風險哨 | `morning-risk` | 週一至五 08:30 | `scripts/ETF早盤風險哨.command` |
| ② | 收盤持股雷達 | `evening-holdings` | 週一至五 16:30 | `scripts/ETF收盤持股雷達.command` |
| ③ | 週日深度補庫 | `weekly-deep` | 週日 20:00 | `scripts/ETF週日深度補庫.command` |

全量除錯：`scripts/ETF每日同步.command` → `daily_sync.sh --quiet`（無 profile）。

## 底層指令

```bash
cd "<project-root>"
export SYNC_PROFILE=morning-risk   # 或 evening-holdings；由 .command 設定
scripts/daily_sync.sh --market-only --quiet   # ①
scripts/daily_sync.sh --holdings-only --quiet # ②
scripts/weekly_sync.sh                        # ③
```

## 產出

| 項目 | 路徑 |
|------|------|
| DB | `data/stocks.db` |
| 平日 log | `logs/daily_sync_YYYYMMDD.log`（①② 同日追加） |
| 週 log | `logs/weekly_sync_YYYYMMDD.log` |

## 自動化

- **僅本機**：Mac `launchd` 指向 `.command` 或 `daily_sync.sh`。
- **不做**：GitHub Actions、n8n、Supabase（已封存於 `archive/docs/cloud-sync-plan.md`）。

## 相關

- [docs/daily-operations.md](../../../docs/daily-operations.md)
- [docs/PRD.md](../../../docs/PRD.md) §5.2
- Skill：`hybrid-market-data-sync`（資料來源與 sync 細節）
