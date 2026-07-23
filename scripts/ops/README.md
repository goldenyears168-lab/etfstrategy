# scripts/ops — 每日營運入口

排程與收盤鏈會呼叫的腳本。回測／sweep 見 [`../research/README.md`](../research/README.md)。

**Pipeline SSOT**：[`config/pipeline_scripts.yaml`](../config/pipeline_scripts.yaml) · [`docs/research-script-inventory.md`](../docs/research-script-inventory.md)

## Shell / 排程

| 檔案 | 用途 |
|------|------|
| `daily_sync.sh` | ② 收盤持股雷達主鏈 |
| `weekly_sync.sh` | 週日深度補庫（**已退役排程** · 僅手動） |
| `backfill_market_data.sh` | 歷史行情補庫 |
| `install-launchd.sh` | launchd 安裝（14 支 job · 清單見下） |
| `install-etfedge-import-launchd.sh` | ETFEdge import |
| `launchd/*.command` | launchd 包裝 |
| `*notify*.sh`, `job_notify.sh` | 排程郵件通知 |

### 現行 launchd job（`install-launchd.sh` LABELS · 僅 mini live）

> **Live 排程 SSOT**：[deploy/mac-mini/MIGRATION_PLAN.md §0](../../deploy/mac-mini/MIGRATION_PLAN.md)（時間／開關以該文為準）。

- 盤中 Order／觀測：`rrg-c18acc-poll`（live）· `leading-dip-poll`（live）· `songshan-copytrade-poll`（live）· `timed-limit-orders`（live once）· `expert-pool-staged-gate`（live）· `detach-gate`（RED 只寄信 · `ORDER_ENABLED=0`）· `buy-signal-radar`／`sell-signal-radar`（不送單）
- 晨間／夜間（不下單）：`crash-thermometer-daily` 09:00 · `branch-tape-prewarm` 18:30 · `winbond-expert-pool-watch` 20:00 · `expert-pool-chart-digest` 20:05 · `holdings-branch-sell-monitor` 20:10 · `second-disp-expert-pool-watch` 20:35
- 另有 `order-wake`（`install-order-launchd.sh` · 防睡眠）

夜間對照：`winbond-expert-pool-watch`＝20:00 買方共識；`holdings-branch-sell-monitor`＝20:10 富邦持倉×專家淨賣＋跨池面板 K/N≥5000萬（淺色 HTML · `scripts/order/run_holdings_branch_sell_monitor.py` · 不下單）。

## Python · daily

| 檔案 | Strategy ID |
|------|-------------|
| `run_market_breadth_report.py` | Breadth zone HTML（研究 · 非 digest） |
| `run_vcp_funnel_specs_daily_brief.py` | `vcp-pivot-gate` · `vcp-coil-close` |
| `run_copytrade_l1h9_daily_brief.py` | `00981a-l1h9` · 收盤訊號篩選 |
| `backfill_vcp_funnel_screen.py` | DB backfill |
| `run_rrg_mono_daily_brief.py` | `rrg-mono-hold7` |
| `import_etfedge_holdings.py` | ETFEdge 持股 |
| `notify_job_result.py` | 通知 helper |

## macOS `.command`

`1630收盤雷達.command` · `2000週日補庫.command`

## Private ops console（好時量化 · Supabase `ops.*`）

前端：https://haoshi-quant-ops.pages.dev · 封存索引見 [`archives/PUBLIC_SITE_RETIRED.md`](../../archives/PUBLIC_SITE_RETIRED.md)。

| 腳本 | 用途 | mini 建議 |
|------|------|-----------|
| `scripts/order/run_ops_live_ta_poll.py` | 處置 ~20分撮合狀態 → `ops.live_ta` | launchd 模板 `ops-live-ta-poll`（45s） |
| `scripts/order/run_ops_holdings_sync.py` | 富邦持倉 → `ops.holdings` | `.venv-fubon` · 手動／晨間 |
| `scripts/order/write_ops_digest_from_file.py` | 檔案／正文 → `ops.digests` | 並行上牆 |
| `scripts/order/write_ops_sleeve_status.py` | `order.yaml`+env → `ops.sleeve_status` | 開盤前一次 |
| `scripts/order/write_ops_console_snapshot.py` | watch／risk／thermo／branches／today → `ops.snapshots` | 手動 one-shot（無 Book launchd） |
| `scripts/notify_job_result.py` | email **且**（`RUN_OPS_DIGEST_SYNC=1`）寫 `ops.digests` | 既有 job_notify |

Helpers：`src/ops_console_sync.py` · `src/ops_live_ta.py`。勿在 Book 安裝 live launchd。

寫入走 `public.ops_*` view（PostgREST 未 expose `ops` schema）· DB 有 INSTEAD OF trigger 轉入 `ops.*`。
