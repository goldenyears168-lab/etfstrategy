---
name: scheme-c-local-ops
description: >-
  Run ETF research local schedules (Scheme C): morning-risk, evening-holdings,
  weekly-deep via scripts/*.command and daily_sync.sh. Use when setting launchd,
  debugging logs, or choosing which sync profile to run on Mac.
disable-model-invocation: true
---

# 方案 C · 本機排程

## 四支排程

| # | 名稱 | slug | 時間 | 入口 |
|---|------|------|------|------|
| ① | 執行評估 | `execution-eval` | 週一至五 08:30 | `scripts/0830執行評估.command` |
| ② | 收盤持股雷達 | `evening-holdings` | 週一至五 16:30 | `scripts/1630收盤雷達.command` |
| ③ | 週日深度補庫 | `weekly-deep` | 週日 20:00 | `scripts/2000週日補庫.command` |
| ④ | 策略回顧 | `signal-review` | — | **已退役**（見 `scripts/策略回顧.command`） |

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
| ETF 日報 | `reports/daily/etf-daily/daily_brief.md` |
| Regime 日報 | `reports/daily/regime/daily_brief.md` |

## 自動化

- **僅本機**：Mac `launchd` 以 `open -gj` 觸發 `scripts/launchd/*.command`（無 `read`、避開 Documents TCC）；手動仍用 `scripts/*執行評估.command` 等。
- **安裝排程**：`scripts/install-launchd.sh`（範本在 `launchd/*.plist.template`）
- **卸載**：`scripts/install-launchd.sh --uninstall`
- **不做**：GitHub Actions、n8n、Supabase 雲端同步（已棄用；現行本地 SQLite + launchd）。

## 相關

- [docs/daily-operations.md](../../../docs/daily-operations.md)
- [docs/PRD.md](../../../docs/PRD.md) §5–§10（Regime · backtest spec · 已退役項）
- Skill：`hybrid-market-data-sync`（資料來源與 sync 細節）
