# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

台股量化交易 Research OS：本地 SQLite (`data/stocks.db`) + market data ingest + 多軌 alpha 策略 + 每日 Facts/Regime 診斷 + Mac mini 自動下單執行層。程式碼 Python 3.13，報告與文件以繁中為主。

先讀 `docs/agent-brief.md`（任務→先讀→可能改對照表），再依任務讀 1–3 個檔案，不要整檔 grep `docs/`。

---

## 常用指令

```bash
# 環境（主 venv；.venv-fubon 專供 Fubon Neo SDK）。qlib/TW100-ML 線已退役（2026-08）
source .venv/bin/activate

# Lint（CI 同款；只檢 E9/F63/F7/F82 語法與未定義名稱）
.venv/bin/ruff check src tests

# 測試 — production（排除 archived backtest）
.venv/bin/pytest tests/ --ignore=tests/research/archive -q
# 單檔 / 單測（pyproject 已設 pythonpath=["src"]，pytest 不需 PYTHONPATH）
.venv/bin/pytest tests/test_analytics_bench.py -q
.venv/bin/pytest tests/test_analytics_bench.py::BenchCloseTest::test_x -q
# CI 實際跑的是 unittest + coverage（fail_under=55）
PYTHONPATH=src .venv/bin/coverage run -m unittest discover -s tests -q && .venv/bin/coverage report

# 收盤管線
SYNC_PROFILE=slim scripts/daily_sync.sh --holdings-report   # 只跑 ingest + Facts + Regime
scripts/1630收盤雷達.command                                  # 完整收盤（沿用 .env RUN_*）

# 健檢
PYTHONPATH=src .venv/bin/python src/pipeline_gates.py list-mismatches   # registry vs RUN_* 不一致
PYTHONPATH=src .venv/bin/python scripts/supabase_health_check.py        # 收盤產物是否 stale
```

手寫腳本一律 `PYTHONPATH=src .venv/bin/python scripts/...`（`src/` 是 flat import root，不是 package）。

---

## 兩台機器的分工（硬邊界）

同一份 repo 在兩台 Mac 各有獨立 checkout。**動手前先確認自己在哪台**：

```bash
scutil --get ComputerName   # 「minim4的Mac mini」= 主力機（開發＋生產）；MacBook = 涼快備援
launchctl list | grep -c goldenstocks   # >0 表示這台掛著 live 排程 → 是 mini
```

**mini 是唯一工作站兼生產機**：日常改碼、研究、測試、git commit／push 都在 mini 做（透過 SSH 遠端操作即可，讓 MacBook 保持涼快、不發燙降頻）。Book 只是 pull-only 的異地備援。

| Intent | Machine |
|--------|---------|
| 改碼、研究、測試、文件、git commit **＋ push** | **Mac mini**（主力機；唯一 push 來源） |
| Live launchd、送單、生產 `.env`／`data/`／ledger、盤中 log | **Mac mini only** |
| 只 `git pull` 保持備援（不 commit、不 push、不裝 launchd） | **MacBook** |

- **Git 方向**：mini push、Book pull（單向）。mini 用 GitHub **deploy key（Read/write）**推 origin；Book 只 `git pull --ff-only`，**永不**在 Book commit／push，以免兩台分叉。single source of truth = mini。
- **Branch 政策**：mini 一律**直接 commit 到 `main`，不開 feature branch**（兩機單線模型；Book `pull --ff-only main`，多開 branch 只會把單線切碎）。**不要**套用「在 default branch 上先開 branch」的通用 agent 預設——本 repo 不走 PR-based 流程。
- 在 **mini** 上改碼會直接影響下次排程 run，動 `scripts/`／`launchd/`／`config/order.yaml` 前先想清楚；不要為了測試改實彈旗標。commit 前先 `git status` 確認沒把生產態誤 `git add`（`.env`／`data/`／`logs/`／`*.db`／`CAFubon/**` 憑證均已 gitignored，但仍別無腦 `git add -A`）。
- **絕不**在 Book 安裝 live `com.jackm4.goldenstocks.*` launchd；不雙跑 Order launchd。這條硬邊界不因「mini 當開發機」而放寬。
- 生產 SQLite SSOT 在 mini `${GOLDENSTOCKS_DATA_DIR}/data/stocks.db`（~40GB + WAL，**預設唯讀查詢**，寫入／`VACUUM` 會鎖住正在跑的排程）；Book 只有 replica，兩份不同步是刻意設計。
- `market_vix_daily` **現在 mini 也有**（mini 自算 TAIWAN VIX / VIXTWN，見 commit `92ae3cb`；VIX+VIXTWN，2026-07-31 仍 fresh），不再是 book-only。
- 機密（`.env`、`CAFubon/`、token）只走 scp/rsync，不進 git、不貼進聊天、不 echo 出值。

**`GOLDENSTOCKS_DATA_DIR`**：可變狀態根目錄（`.env`、`data/`、`logs/`）可搬出 git tree（mini 為 `~/goldenstocks-data`）。新程式碼讀寫 DB／log／ledger 一律走 `stock_db.DATA_DIR` / `DEFAULT_DB_PATH` / `project_dotenv`，**不要**硬寫 `PROJECT_ROOT / "data"`（近期多筆 commit 就是在補這件事）。

---

## 分層架構

### Product layers（≠ `src/` L0–L5）

`facts` → `regime` → `research` → `strategy`（+ 本地 `order`）

| Layer | SSOT config | 不是 |
|-------|-------------|------|
| Facts | `etf_daily_report` 產物 | 評分／選股 |
| Regime（環境層） | `config/regime.yaml`（四軸診斷） | alpha · live gate |
| Research | `config/research.yaml`（探索主題 · sweep · graduation gates G1–G*） | 凍結規格 |
| Strategy | `config/strategy.yaml`（採納凍結規格）+ `config/strategies.yaml`（registry/publish） | ensemble 加權 |
| Order（下單層） | `config/order.yaml` · `src/order/` | 策略 import 鏈 |

研究主題**採納**（不叫「畢業」）後才寫進 `config/strategy.yaml`；同一 `strategy_id` 的 `enabled` 在 `strategy.yaml` 與 `strategies.yaml` 必須一致。

### `src/` L0–L5（SSOT：`docs/src-map.md`）

L0 Platform（`stock_db`、`project_config`、`report_paths`、`finmind_client`、`pipeline_gates`、`strategy_registry`）→ L1 Ingest（`sync_*`、`backfill_*`）→ L2 Domain（`market_*`、`flow_*`、`regime_snapshot`、`analytics/bench`）→ L3 收盤 pipeline（`etf_daily_report`、`regime_daily_brief`）→ L4 Tracks（launchd daily brief / screen）→ L5 Research（`research/backtest/*`）。

Import 規則：
1. `src/order/` **不得**被策略／research 腳本 import；下單層只讀 `reports/order/intents/*.json`（schema `order-intent-v1`）。
2. L3 daily pipeline 不得 import `research.backtest.*`。
3. ⚠️ 規則 2 目前**未被強制**：約 19–20 支 L4 模組實際有 import `research.backtest.finpilot_local_backtest.load_price_panels`（純 DB→wide DataFrame helper）。動這塊前先讀 `docs/src-map.md` 末段的健檢註記；不要以為文件＝現況。

### 收盤主線

```
query_stock_prices → sync_etf_holdings → etf_daily_report → regime_daily_brief
  → reports/daily/etf-daily/daily_brief.md
  → reports/daily/regime/daily_brief.md
```

Gate 兩層且皆須通過：`config/strategies.yaml` 的 `enabled`（registry gate）**且** 對應 `RUN_*` 環境變數。`SYNC_PROFILE=slim` 會覆寫關掉 RRG / VCP close / Lens / Supabase sync。Gate 邏輯 SSOT 在 `src/pipeline_gates.py` 的 `_DAILY_SYNC_STEPS`；加新步驟要同步改這裡，否則 `list-mismatches` 會叫。

---

## Config SSOT 速查

| 用途 | 檔案 |
|------|------|
| 收盤 DAG | `config/pipelines/daily_close.yaml` |
| 策略 registry · enabled · publish 路徑 | `config/strategies.yaml` |
| 採納規格 · backtest 契約 | `config/strategy.yaml` |
| 探索主題 · sweep · graduation gates | `config/research.yaml` |
| 跨軌比較層（notional／NAV engine／adapter） | `config/backtest_standard.yaml` |
| Regime 四軸 | `config/regime.yaml` |
| 下單意圖 · sleeve | `config/order.yaml` |
| **mini 現掛 launchd job／能否送單** | `config/job_registry.yaml` |
| Pipeline / launchd 腳本 registry | `config/pipeline_scripts.yaml` |
| Gate · `RUN_*` | `src/pipeline_gates.py` · `.env`（範本 `.env.example`） |

---

## Order layer（下單層）— 安全預設

`config/job_registry.yaml` 是「裝了什麼、能不能送單」的唯一 SSOT，勿只信 `docs/daily-operations.md` 舊表。

現況：4 支 order-capable job（`leading-dip-poll`／`songshan-copytrade-poll`／`expert-pool-staged-gate`／`detach-gate`）全部三重鎖住 ——
1. `.env` 旗標本身安全（`ORDER_*_DRY_RUN=1` / `ORDER_*_ENABLED=0`）
2. `launchctl disable`（重開機／重裝不復活）
3. `.env` 總開關 `ORDER_MASTER_ENABLED=0`（`src/order/fubon_orders.py` 檢查）

改動下單相關程式時**維持 dry-run／disabled 為預設**：plist template 與 launcher template 內的 `ORDER_*_DRY_RUN` 一律 `1`、`ORDER_*_AUTO_SUBMIT` 一律 `0`，實彈只由 mini 的 `.env` 開。恢復送單能力必須是使用者明確直接的指示。

**ABC Order 軌已退役**（2026-07-16）；`buy-signal-radar` 只寄信、不送單。
**C18acc 排程已退役**（2026-08-04 · 策略不再採用 · 主要動機是不再收它的失敗信）。安靜是靠三層，**不是**靠改程式：
1. launchd 移除 —— plist／launcher／`.command` 從 repo 與 mini 刪除，label 進 `RETIRED_LABELS`，重跑安裝腳本只會再卸載一次、不會復活
2. `config/strategies.yaml`＋`config/strategy.yaml` 的 `rrg-mono-swap-accel`／`-extension` 設 `enabled: false`（registry gate 擋掉收盤 brief）
3. `.env` 的 `RUN_RRG_C18ACC_SCREEN`／`_EMAIL`／`RUN_C18ACC_POOL_DIGEST_EMAIL`／`RUN_RRG_MONO_SWAP_ACCEL_DAILY` 全設 0

**程式碼與 `config/order.yaml` 規格一律未動**（`src/order/c18acc_*.py`、screen、research／backtest 線都在），要研究時手動跑 `scripts/run_rrg_mono_swap_accel_screen.py` 即可。退役時尚有 3 個未平槽位（2377／2103／4167），現為手動持倉。

⚠️ 關掉 `rrg-mono-swap-accel` 的副作用（已處理，但值得記住這個模式）：`rrg_universe_close` 原本 gate 是 `hold7 OR swap-accel`（`match: any`），而 hold7 早已 disabled，所以 swap-accel 是**唯一**還撐著它的——一關就會讓 `rrg_universe_scores` 的 `screen_kind='close'` 停止寫入，而它有兩個活的消費者：

- `capitulation-oos-accumulate`（launchd · 平日 19:00）取 `MAX(session_date) WHERE screen_kind='close'`，停寫後會凍在舊日期、每天拿同一份舊快照繼續累積 OOS 事件——**不會報錯**，但會污染 OOS ledger
- `stock_daily_lens`（收盤 · `RUN_STOCK_DAILY_LENS=1`）呼叫 `load_rrg_universe_scores(conn, date, "close")` 三次 → 上網站

已把該步從 registry gate 解耦（`src/pipeline_gates.py` · `strategy_ids: ()`，純 infra 只吃 `RUN_RRG_UNIVERSE_CLOSE`），並補測試釘住。**教訓**：退役策略前先查 `_DAILY_SYNC_STEPS` 裡有沒有 `match: any` 的步驟把它列為 parent——那種 gate 會在最後一個 parent 被關掉時靜默熄火。

---

## launchd 慣例

- 版控的是 `launchd/*.plist.template` 與 `launchd/*-launcher.sh.template`（`{{PROJECT_ROOT}}`／`{{APP_SUPPORT}}`／`{{HOME}}`／`{{*_CALENDAR_INTERVALS}}` 佔位符）。
- `scripts/install-launchd.sh` 渲染後裝到 `~/Library/LaunchAgents/` + `~/Library/Application Support/com.jackm4.goldenstocks/`；新增／退役 job 要同時改 `LABELS`、`TEMPLATES`（退役的加進 `RETIRED_LABELS`／`LEGACY_LABELS` 讓下次執行自動卸載），並更新 `config/job_registry.yaml`。
- launcher 自己做「星期／時窗」過濾（plist 只給 5 分鐘鐘面）、用 `mkdir` lockdir 防重疊、`source` `${GOLDENSTOCKS_DATA_DIR}/.env`。祕密**不要**寫進 plist。
- 退役 job 時，除了移進 `RETIRED_LABELS`，**也要刪掉 `render_template` 內對應的 `{{*_LAUNCHER}}` sed 行** —— 那些行引用已不再初始化的變數，`set -u` 會讓整支安裝腳本在第一次 render 就死（2026-08-01 退役 sell-signal-radar／ops-live-ta-poll 時就發生過，2026-08-02 修好）。
- 唯一的 AI agent job：`mini-schedule`（每日 08:30 · headless `claude -p` 資料體檢 · 唯讀 DB · 不下單）。判準 SSOT 是 `scripts/launchd/mini-schedule-prompt.txt`，改 prompt **不需**重裝 launchd；cwd 刻意留在 `${GOLDENSTOCKS_DATA_DIR}`，因為該目錄的 `CLAUDE.md` 才是它的護欄。
- Log：盤中 `logs/intraday/`、收盤／週末 `logs/`、富邦 SDK `log/`（後者非 daily_sync log）。

---

## 術語規範（提交前必查）

SSOT：`docs/terminology.md`（§7 Deprecated、§10 quick reference）。完整 Use→Don't use 表在 `.cursor/rules/terminology-glossary.mdc`，套用於 `reports/**`、`docs/**`、`*.md`。

- Prose 首次出現用 `English term（中文術語）`；**code / config identifier 一律英文**。
- 高頻禁語：**Regime layer（環境層）／四軸市場環境** 不寫「市場體制／體制診斷層」；**Trend posture** 不寫 `regime_name`／「趨勢階段」；**Order layer（下單層）** 不寫「執行層」／`layer: execution`；research→strategy 叫**採納**不叫「畢業」；**Minervini** 不音譯。
- 交付物是**結果**（檔案、DB、決策），不是「跑過腳本」。PIT：訊號日 T 只能用 `date ≤ T` 的資料。

回答分析比較、策略檢視、研究摘要時，**直接把完整答案寫在聊天回覆裡**（markdown 表格／條列），不要產生 canvas。

---

## 文件導航

| 想知道 | 看這份 |
|--------|--------|
| 5 分鐘看懂機器／資料／排程全貌 | `docs/system-overview.md` |
| 任務→先讀→可能改（省 token） | `docs/agent-brief.md` |
| 產品分層、收盤主線、SSOT 表 | `docs/architecture.md` |
| `src/` L0–L5 與 import 規則 | `docs/src-map.md` |
| 排程 SOP、profile 對照、Supabase sync | `docs/daily-operations.md` |
| 下單層完整規格 | `docs/order-layer-prd.md` |
| Backtest spec / per-track JSON | `docs/evaluation-contract.md` · `docs/unified-backtest-standard.md` |
| FinMind 取數（dataset 對照、rate limit） | `.cursor/rules/finmind.mdc` |
| 專家池 mini 操作凍結規則 | `MINI_OPS_REFERENCE.md` |
| **狀態根目錄**（`data/` 佈局、log／備份／鎖慣例、各表預期資料輪廓、mini 每日體檢 job） | `${GOLDENSTOCKS_DATA_DIR}/CLAUDE.md`（不在 git；cwd 在資料目錄時才會自動載入） |

`docs/` 約 28 份含階段性研究筆記、無嚴格閱讀順序；有疑問優先 `docs/PRD.md`、`docs/daily-operations.md`、`config/job_registry.yaml`。
