# 每日排程速查（infra SOP）

> **非 Facts product layer** — 本文件是 launchd / 手動腳本排程；事實產物見 `reports/daily/etf-daily/`（**Facts layer** · `layer: facts`）。  
> 架構：[architecture.md](./architecture.md) · 術語：[terminology.md](./terminology.md)  
> **Mac mini Order layer（含跟單松山／C18／Leading Dip／EP gate）SSOT**：[config/job_registry.yaml](../config/job_registry.yaml)（送單開關以該檔為準，勿只信本表舊列）。

## daily_sync profile · Gate 規則

**收盤管線 SSOT**：`scripts/daily_sync.sh` · `src/pipeline_gates.py` · `config/strategies.yaml`

| 機制 | 說明 |
|------|------|
| **`SYNC_PROFILE=slim`** | 載入 `.env` 後覆寫：關 RRG / VCP close / Lens / Supabase brief sync；**只保證** ingest + **Facts** + **Regime** |
| **`SYNC_PROFILE=full`** | 不覆寫；沿用 `.env` 各 `RUN_*`（`.env.example` 預設） |
| **Registry gate** | `config/strategies.yaml` · `enabled=false` → 對應策略步驟 **SKIP**（即使 `RUN_*=1`） |
| **`RUN_*` gate** | `RUN_*=0` → SKIP（在 registry 已啟用時才會跑到） |

```bash
# 僅 Facts + Regime（本地研究、不看策略 brief）
SYNC_PROFILE=slim scripts/daily_sync.sh --holdings-report

# 完整收盤（沿用 .env）
scripts/1630收盤雷達.command
```

**啟用策略軌**：同時設 `config/strategies.yaml` + `config/strategy.yaml` · `enabled: true`（兩檔一致），並開對應 `RUN_*`。

> **退役策略前先檢查**：`src/pipeline_gates.py` 的 `_DAILY_SYNC_STEPS` 內有沒有 `match: any` 的步驟把該策略列為 parent。那種 gate 在**最後一個** parent 被關掉時會靜默熄火。2026-08-04 退 `rrg-mono-swap-accel` 時就踩到——它是 `rrg_universe_close` 唯一還活著的 parent，而 `capitulation-oos-accumulate`（19:00）與 `stock_daily_lens` 都在吃該步產出的 `screen_kind='close'` 資料。該步已改為純 infra（`strategy_ids: ()`）。

**健康檢查**（registry 與 `RUN_*` 不一致時 WARN）：

```bash
PYTHONPATH=src .venv/bin/python scripts/supabase_health_check.py
PYTHONPATH=src .venv/bin/python src/pipeline_gates.py list-mismatches
```

### Profile 對照（16:30 `daily_sync`）

| 步驟 | slim | full（`.env.example`） | Registry `strategy_id` |
|------|------|------------------------|-------------------------|
| ingest + 持股 + **etf-daily** + **regime-daily** | ✓ | ✓ | `etf-daily` · `regime-daily`（恆 `enabled: true`） |
| RRG universe close snapshot | ✗ | ✓ | **無 registry gate**（純 infra · 只吃 `RUN_RRG_UNIVERSE_CLOSE`） |
| RRG mono / swap-accel brief | ✗ | ✓ | `rrg-mono-hold7` · `rrg-mono-swap-accel`（兩者皆已 `enabled: false`） |
| L1H9 copytrade screen | ✓ | ✓ | `00981a-l1h9` |
| VCP funnel close | ✗ | ✓ | `vcp-pivot-gate` / `vcp-coil-close` |
| stock_daily_lens → Supabase | ✗ | ✓ | （跨層 publish · 僅 `RUN_STOCK_DAILY_LENS`） |

### 日誌目錄

| 路徑 | 用途 |
|------|------|
| **`logs/intraday/`** | 盤中排程：buy/sell radar · c18 poll · exit gate · digest · 08:50 brief · 13:00 VCP/RRG |
| **`logs/`** | 收盤／週日等非盤中：`daily_sync_YYYYMMDD.log` · `launchd_evening-holdings.log` · `weekly_sync_*.log` |
| **`log/`** | 富邦 Neo SDK / 本機 client（`.gitignore`）；**非** daily_sync 主 log |

## 排程

> **Live launchd 排程 SSOT**：[config/job_registry.yaml](../config/job_registry.yaml)（mini 現掛清單）。下表含已退役／手動入口，僅供速查。

| # | 名稱 | 時間 | 入口 |
|---|------|------|------|
| VCP | Pivot Gate / Coil Close · 盤中 screen+brief | —（**已退役排程** · 僅手動） | `scripts/launchd/vcp-funnel-specs.command` |
| VCP′ | Pivot Gate / Coil Close · 收盤 screen+brief | 16:30 | `scripts/daily_sync.sh`（`RUN_VCP_FUNNEL_CLOSE=1`） |
| `minervini-sepa-basket` | `minervini_sepa_daily` | —（**已退役排程** · 僅手動） | `scripts/launchd/minervini-sepa-basket.command` |
| `buy-signal-radar` | C0 買進 advisory | **已停用**（原 09:00–13:20/5分） | `scripts/launchd/buy-signal-radar.command` |
| `sell-signal-radar` | Fubon 持倉 extension 賣出 advisory | **已退役**（見 job_registry） | `scripts/launchd/sell-signal-radar.command` |
| `rrg-c18acc-poll` | C18acc 開倉／換倉 | **排程已退役**（2026-08-04 · plist／launcher／`.command` 已刪 · registry + `.env` 旗標全關） | 手動：`scripts/run_rrg_mono_swap_accel_screen.py`（程式碼未動） |
| `leading-dip-poll` | Leading Dip 衛星袖 | **已停用·三重鎖**（原 09:05–13:25/5分） | `scripts/launchd/leading-dip-poll.command` |
| `songshan-copytrade-poll` | 跟單松山（5d淨比95∩!mega + 25m nonfail · 1 張） | **已停用·三重鎖**（原 09:25–09:40/5分） | `scripts/launchd/songshan-copytrade-poll.command` · 現況見 [config/job_registry.yaml](../config/job_registry.yaml) |
| `expert-pool-staged-gate` | 專家池 gap→05→25（≠ 松山尺） | **已停用·三重鎖**（原 09:00／01／05／25） | `scripts/launchd/expert-pool-staged-gate.command` |
| `rrg-mono-swap-accel-extension` | extension overlay（legacy · 手動） | — | `scripts/run_c18acc_extension_screen.py` |
| ②a | RRG mono 收盤前預警 + universe snapshot | —（**已退役排程** · 僅手動） | `scripts/launchd/rrg-mono-intraday-watch.command` |
| ② | 收盤 ETF 日報（含 RRG universe close + mono 槽位 + **stock_daily_lens**） | 16:30 | `scripts/1630收盤雷達.command` |
| ②b | 安聯台灣科技基金（ACDD04）月報公布偵測 | —（**已退役排程** · 僅手動） | `scripts/launchd/mutual-fund-disclosure-watch.command` |
| ③ | 週日補庫（**已退役排程** · 僅手動） | — | `scripts/2000週日補庫.command` |

## Supabase 自動同步（`RUN_SUPABASE_RESEARCH_SYNC=1` · `RUN_SUPABASE_LENS_SYNC=1`）

| 表 | 內容 | 排程 | 開關 |
|----|------|------|------|
| `daily_briefs` · slot `1300` | VCP funnel / Pivot Gate / Coil Close · RRG 盤中預警 | 13:00 launchd · 16:30 再推（VCP 收盤覆寫） | `RUN_SUPABASE_RESEARCH_SYNC` |
| `daily_briefs` · slot `1630` | ETF 日報 · Regime · RRG mono 收盤 · Copytrade L1H9 | 16:30 `daily_sync` | `RUN_SUPABASE_RESEARCH_SYNC` |
| `daily_briefs.snapshot_json` | `etf-daily-v1` · `regime-snapshot-v1` · **`vcp-daily-v1`** | sync 時預算 | — |
| `rrg_universe_scores` | RRG 成分股象限（`intraday` / `close`） | 13:00 / 16:30（Python 內建） | `RUN_SUPABASE_RESEARCH_SYNC` |
| `stock_daily_lens` · `lens_daily_alert` | 跨層 Lens · 當日 headline | 16:30 `daily_sync` | `RUN_SUPABASE_LENS_SYNC`（launchd 預設 1） |
| `site_content` | 六層靜態頁 · 策略 registry · 採納報告 · catalog 長文 | Readdy 直連 Supabase · authoring 見 [readdy-regime-strategy-lineage.md §7.4](../archives/RETIRED_readdy-regime-strategy-lineage.md) | — |
| `strategy_performance_yearly` | 已採納策略分年績效 | **手動** `scripts/sync_strategy_performance.py` 或 `RUN_STRATEGY_PERF_SYNC=1` | `RUN_STRATEGY_PERF_SYNC` |

> `daily_briefs.snapshot_json`：`regime_daily` → `regime-snapshot-v1` · `etf_daily` → **`etf-daily-v1`**（Readdy 直讀，勿 parse MD）。`content_html` 不再 sync。

> Migration **013**（registry 欄位）已部署 · 驗證 SQL 見 [readdy-regime-strategy-lineage.md §7.0](../archives/RETIRED_readdy-regime-strategy-lineage.md)。

**收盤後健康檢查**（公開站是否 stale）：

```bash
PYTHONPATH=src .venv/bin/python scripts/supabase_health_check.py --notify
```

檢查：`daily_briefs`（1300/1630）· `stock_daily_highlight` · `daily_highlight_alert` · 五軌 `site_content` registry · `RUN_*` 開關 · **pipeline registry 對齊**。FAIL 時 exit 1；`--notify` 送 macOS 通知。

1. **`reports/daily/etf-daily/daily_brief.md`** — 各 ETF 持股變化（00981A 新进/加码 等）
2. **`reports/daily/regime/daily_brief.md`** — Regime 四格雷達
3. **`reports/research/breadth/*_market_breadth_ma_*.html`**（可選）— Breadth zone
4. `reports/daily/vcp_funnel_specs_daily_brief.md`（13:00 盤中預估 · 16:30 收盤確認覆寫）
5. `reports/daily/rrg_mono_intraday_watch.md`（13:00 後，候選預警）
6. `reports/daily/rrg_mono_daily.md`（16:30 後，收盤確認 · 併入 daily_sync）
7. **Supabase `stock_daily_lens` + `lens_daily_alert`**（16:30 尾段 · `RUN_STOCK_DAILY_LENS=1` · `RUN_SUPABASE_LENS_SYNC=1`）

## ② 收盤 Lens（網站）

- 表：`stock_research.stock_daily_lens` · `lens_daily_alert`
- 手動：`PYTHONPATH=src .venv/bin/python scripts/run_stock_daily_lens.py`
- Email：`RUN_LENS_DAILY_NOTIFY=1`（見 `.env`）

## 手動研究

- **00981A L1H9 跟單回測**：`scripts/run_00981a_copytrade_backtest.py --strategy L1H9`
- 方法論：`docs/00981a-copytrade-research-methodology.md`

## `.env`（摘）

見 [`.env.example`](../.env.example) · **`SYNC_PROFILE=slim|full`** · **`RUN_*`** · **`config/strategies.yaml` · `enabled`** 三者須一致。

```bash
# slim：僅 Facts + Regime
SYNC_PROFILE=slim scripts/daily_sync.sh

# 已退役
RUN_SCORE_ENGINE=0
```

（`RUN_SCORE_ENGINE` 已退役；收盤核心為 etf-daily + regime-daily。策略軌另受 registry gate 約束。）
