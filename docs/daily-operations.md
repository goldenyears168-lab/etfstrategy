# 每日排程速查（infra SOP）

> **非 Facts product layer** — 本文件是 launchd / 手動腳本排程；事實產物見 `reports/daily/etf-daily/`（**Facts layer** · `layer: facts`）。  
> 架構：[architecture.md](./architecture.md) · 術語：[terminology.md](./terminology.md)

## 排程

| # | 名稱 | 時間 | 入口 |
|---|------|------|------|
| VCP | Pivot Gate / Coil Close · 盤中 screen+brief | 13:00 | `scripts/launchd/vcp-funnel-specs.command` |
| VCP′ | Pivot Gate / Coil Close · 收盤 screen+brief | 16:30 | `scripts/daily_sync.sh`（`RUN_VCP_FUNNEL_CLOSE=1`） |
| ②a | RRG mono 收盤前預警 + universe snapshot | 13:00 | `scripts/launchd/rrg-mono-intraday-watch.command` |
| ② | 收盤 ETF 日報（含 RRG universe close + mono 槽位 + **stock_daily_lens**） | 16:30 | `scripts/1630收盤雷達.command` |
| ②b | 安聯台灣科技基金（ACDD04）月報公布偵測 | 16:30 | `scripts/launchd/mutual-fund-disclosure-watch.command` |
| ③ | 週日補庫 | 週日 20:00 | `scripts/2000週日補庫.command` |

## Supabase 自動同步（`RUN_SUPABASE_RESEARCH_SYNC=1` · `RUN_SUPABASE_LENS_SYNC=1`）

| 表 | 內容 | 排程 | 開關 |
|----|------|------|------|
| `daily_briefs` · slot `1300` | VCP funnel / Pivot Gate / Coil Close · RRG 盤中預警 | 13:00 launchd · 16:30 再推（VCP 收盤覆寫） | `RUN_SUPABASE_RESEARCH_SYNC` |
| `daily_briefs` · slot `1630` | ETF 日報 · Regime · RRG mono 收盤 · Copytrade L1H9 | 16:30 `daily_sync` | `RUN_SUPABASE_RESEARCH_SYNC` |
| `daily_briefs.snapshot_json` | `etf-daily-v1` · `regime-snapshot-v1` · **`vcp-daily-v1`** | sync 時預算 | — |
| `rrg_universe_scores` | RRG 成分股象限（`intraday` / `close`） | 13:00 / 16:30（Python 內建） | `RUN_SUPABASE_RESEARCH_SYNC` |
| `stock_daily_lens` · `lens_daily_alert` | 跨層 Lens · 當日 headline | 16:30 `daily_sync` | `RUN_SUPABASE_LENS_SYNC`（launchd 預設 1） |
| `site_content` | 六層靜態頁 · 策略 registry · 採納報告 · catalog 長文 | Readdy 直連 Supabase · authoring 見 [readdy-regime-strategy-lineage.md §7.4](./readdy-regime-strategy-lineage.md) | — |
| `strategy_performance_yearly` | 已採納策略分年績效 | **手動** `scripts/sync_strategy_performance.py` 或 `RUN_STRATEGY_PERF_SYNC=1` | `RUN_STRATEGY_PERF_SYNC` |

> `daily_briefs.snapshot_json`：`regime_daily` → `regime-snapshot-v1` · `etf_daily` → **`etf-daily-v1`**（Readdy 直讀，勿 parse MD）。`content_html` 不再 sync。

> Migration **013**（registry 欄位）已部署 · 驗證 SQL 見 [readdy-regime-strategy-lineage.md §7.0](./readdy-regime-strategy-lineage.md)。

**收盤後健康檢查**（公開站是否 stale）：

```bash
PYTHONPATH=src .venv/bin/python scripts/supabase_health_check.py --notify
```

檢查：`daily_briefs`（1300/1630）· `stock_daily_highlight` · `daily_highlight_alert` · 五軌 `site_content` registry · `RUN_*` 開關。FAIL 時 exit 1；`--notify` 送 macOS 通知。

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

```bash
RUN_SCORE_ENGINE=0
RUN_VCP_FUNNEL=1
```

（`RUN_SCORE_ENGINE` 已退役；收盤主線僅 ETF 日報。）
