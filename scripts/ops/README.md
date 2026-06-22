# scripts/ops — 每日營運入口

排程與收盤鏈會呼叫的腳本。回測／sweep 見 [`../research/README.md`](../research/README.md)。

## Shell / 排程

| 檔案 | 用途 |
|------|------|
| `daily_sync.sh` | ② 收盤持股雷達主鏈 |
| `weekly_sync.sh` | ③ 週日深度補庫 |
| `backfill_market_data.sh` | 歷史行情補庫 |
| `install-launchd.sh` | launchd 安裝 |
| `install-etfedge-import-launchd.sh` | ETFEdge import |
| `launchd/*.command` | launchd 包裝 |
| `*notify*.sh`, `job_notify.sh` | 排程郵件通知 |

## Python · daily

| 檔案 | Strategy ID |
|------|-------------|
| `run_market_breadth_report.py` | Breadth zone HTML（研究 · 非 digest） |
| `run_vcp_funnel_specs_daily_brief.py` | `vcp-pivot-gate` · `vcp-coil-close` |
| `run_copytrade_l1h9_daily_brief.py` | `00981a-l1h9` · 收盤訊號篩選 |
| `backfill_vcp_funnel_screen.py` | DB backfill |
| `run_rrg_mono_daily_brief.py` | `rrg-mono-hold7` |
| `run_factor_validation.py` | 週末可選 |
| `import_etfedge_holdings.py` | ETFEdge 持股 |
| `notify_job_result.py` | 通知 helper |

## macOS `.command`

`1630收盤雷達.command` · `2000週日補庫.command`
