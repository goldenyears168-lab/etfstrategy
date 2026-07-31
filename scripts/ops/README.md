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
> **心智模型（2026-07-23 公告）**：三時段 — **18:30 夜盤準備** · **08:50 開盤前** · **盤中**；另加 **20:00–20:40 夜間觀測波**（當日結論／上牆）。詳見站上 Inbox／知識庫「排程重整公告」。

#### 18:30 · 夜盤準備
- `branch-tape-prewarm` — 分點 tape 補檔（POOLS∪持倉）

#### 20:00–20:40 · 夜間觀測波（當日結論）
- `winbond-expert-pool-watch` 20:00 · `expert-pool-chart-digest` 20:05 · `holdings-branch-sell-monitor` 20:10 · `second-disp-expert-pool-watch` 20:35 · `ops-console-evening-sync` **20:40**
- **郵件政策（2026-07-23）**：20:00–20:35 設 `RUN_ALERT_EMAIL=0`（細節只上 Inbox）；**20:40 寄一封夜間摘要**（`send_ops_evening_summary.py`，含當日溫度計重算）。重大／Detach RED 仍即時另寄。

#### 08:50–09:05 · 開盤前
- `ops-live-ta-poll` 08:50 起 · `crash-thermometer-daily` 09:00（多半昨收、**只算不寄**）· Order 窗開啟

#### 盤中 · Order／觀測
- live：`rrg-c18acc-poll` · `leading-dip-poll` · `songshan-copytrade-poll` · `expert-pool-staged-gate`
- 觀測：`detach-gate`（RED 仍即時寄）· `buy/sell-signal-radar`（不下單）· `ops-live-ta-poll`
- 另有 `order-wake`（`install-order-launchd.sh` · 防睡眠）

夜間對照：`winbond-expert-pool-watch`＝20:00 買方共識；`holdings-branch-sell-monitor`＝20:10 富邦持倉×專家淨賣＋跨池面板 K/N≥5000萬；`ops-console-evening-sync`＝20:40 重算大跌溫度計（當日 asof）→ snapshots／sleeve／holdings → `send_ops_evening_summary.py` 一封摘要。

溫度計時序：09:00 多半**昨收、不寄信**；**當日讀數＋郵件以 20:40 為準**。

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

| 腳本 | 用途 | mini 自動 |
|------|------|-----------|
| `scripts/order/run_ops_live_ta_poll.py` | 持倉 Live TA：現價／短動能／30m＝富邦 Neo → `ops.live_ta` | `com.jackm4.goldenstocks.ops-live-ta-poll`（週一至五 **08:50 觸發一次** · `--loop` 至 13:35 · 現價≈15s · 一登入 · `.venv-fubon`） |
| `scripts/order/write_ops_console_snapshot.py` | watch／risk／thermo／branches／today → `ops.snapshots`（可 `--also-digest`） | `com.jackm4.goldenstocks.ops-console-evening-sync`（20:40） |
| `scripts/order/write_ops_sleeve_status.py` | `order.yaml`+env → `ops.sleeve_status` | 同上 20:40 |
| `scripts/order/run_ops_holdings_sync.py` | 富邦持倉 → `ops.holdings` | 同上 20:40（`.venv-fubon`） |
| `src/notify_email.send_alert` / `scripts/notify_job_result.py` | email **且**（`RUN_OPS_DIGEST_SYNC=1`，預設開）寫 `ops.digests` | 夜間／晨間 email jobs 並行上牆 |
| `scripts/order/write_ops_digest_from_file.py` | 檔案／正文 → `ops.digests` | 手動／補檔 |

Helpers：`src/ops_console_sync.py` · `src/ops_console_snapshots.py` · `src/ops_live_ta.py`。

**環境**：mini `.env` 須有 `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`；`RUN_OPS_DIGEST_SYNC=1`（launchd `ops-console-evening-sync` plist 亦硬設 1）。勿在 Book 安裝 live Order／ops launchd。

**明日自檢（不必手動 backfill）**：`launchctl list | grep ops-console` 有 label；站上 Inbox／Today／Watch／Risk／Thermo／Holdings 的 asof 為當日；盤中 Live 頁跟 `ops-live-ta-poll`。

寫入走 `public.ops_*` view（PostgREST 未 expose `ops` schema）· DB 有 INSTEAD OF trigger 轉入 `ops.*`。
