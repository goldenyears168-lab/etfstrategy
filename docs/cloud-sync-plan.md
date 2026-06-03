# 雲端同步計畫：n8n → GitHub Actions → Supabase

> **狀態（2026-06）：Phase 0 — 本機手動**  
> 先在本機 SQLite 手動跑順，確認資料正確後再進入 Phase 1～3。  
> **現階段不啟用** crontab / Render / n8n 排程 / GitHub Actions。

---

## 目標

| 項目 | 說明 |
|------|------|
| 研究標的 | 5 ETF：`00981A` `00403A` `009816` `00988A` `00407A`（00407A 掛牌前 skip） |
| 雲端正庫 | **Supabase Postgres**（authoritative） |
| 本機副本 | `data/stocks.db`（分析、回測用，pull 下來） |
| 排程 | **n8n Cloud** 觸發（已有訂閱） |
| 計算 | **GitHub Actions** 跑現有 Python repo |
| 額外費用 | GA + Supabase Free ≈ **$0/月**（n8n 為既有成本） |

**不採用 Render Cron**（已有 n8n；GA 可 $0 跑 Python）。

---

## 架構總覽

```
n8n Cloud (Schedule 20:30 / 22:00 TST)
    │  POST workflow_dispatch
    ▼
GitHub Actions (ubuntu runner)
    │  scripts/daily_sync.sh  (or equivalent steps)
    │  query_stock_prices.py  (TEJ 5 ETF + IX0001/IR0002, --skip-watchlist)
    │  sync_etf_signal.py     (FinMind 法人)
    │  sync_etf_holdings.py   (EZMoney 3 + KGIFund 2)
    │  env: DATABASE_URL → Supabase
    ▼
Supabase Postgres
    │  daily_bars, etf_daily_signal_snapshot
    │  etf_holdings, etf_holdings_meta
    │  sync_runs (建議，除錯用)
    ▼
本機 (選用) pull_from_supabase.py → data/stocks.db
```

### 各層職責

| 層 | 工具 | 負責 |
|----|------|------|
| 排程 / 編排 | n8n Cloud | 何時跑、retry 分支、Line/Email 通知 |
| 批次計算 | GitHub Actions | 執行 repo 內 Python（含 EZMoney HTML 解析） |
| 雲端儲存 | Supabase Postgres | 每日 snapshot、行情、持股 |
| 本機研究 | SQLite | pull 副本、`--changes` 加減碼分析 |

### 為何 n8n 不直接跑 Python

n8n Cloud **無法** Execute Command 執行本機/repo 腳本。  
以 **HTTP 觸發 GitHub Actions** 當遠端 runner，沿用現有 `query_stock_prices.py` / `sync_etf_holdings.py`。

---

## 分階段實施

### Phase 0：本機手動（**現在**）

**目標：** SQLite 流程跑順，5 ETF 資料可驗證。

**推薦入口（一次跑 4 項）：**

```bash
# 桌面雙擊：~/Desktop/ETF每日同步.command
# 或：
/Users/jackm4/Documents/ETF/股票研究/scripts/daily_sync.sh
# 單步：--holdings-only / --market-only / --retry
```

**daily_sync 內容：**

1. `query_stock_prices.py --skip-watchlist --etf-codes 00981A,00403A,009816,00988A,00407A` → `daily_bars`
2. `sync_etf_signal.py --etf-codes …` → `etf_daily_signal_snapshot`
3. `sync_etf_holdings.py` EZMoney（3 檔）+ KGIFund（009816；00407A 掛牌前 skip）→ `etf_holdings`
4. `--changes` 輸出（≥2 snapshot 日才有表）

**單步除錯：**

```bash
cd "/Users/jackm4/Documents/ETF/股票研究"
.venv/bin/python sync_etf_holdings.py --etf-codes 00981A,00403A,00988A --source ezmoney --dry-run
.venv/bin/python sync_etf_holdings.py --etf-code 009816 --source kgifund --dry-run
.venv/bin/python sync_etf_holdings.py --etf-codes 00981A,00403A,00988A,009816,00407A --changes
```

**環境：** 專案根目錄 `.env`（不 commit）

```
TEJ_API_KEY=...
FINMIND_TOKEN=...   # 選填
```

**完成標準（Phase 0 exit）：**

- [ ] `.env` 設定正確，hybrid sync 無 TEJ `PARAMERR` / `LMT02`
- [ ] `daily_bars`：5 ETF + IX0001 + IR0002（00407A 掛牌前可無列）
- [ ] `etf_holdings_meta`：EZMoney 3 檔 + KGIFund 009816 有 NAV / holding_count
- [ ] 連續手動跑 **≥2 個交易日**，各 ETF `--changes` 可輸出 share_delta
- [ ] 官網持股股數與 SQLite 可對照
- [ ] `python -m py_compile query_stock_prices.py sync_etf_holdings.py sync_etf_signal.py stock_db.py` 通過

**現階段不做：** crontab、Render、n8n 排程、GitHub Actions、Supabase 連線。

---

### Phase 1：Supabase + 雙模式 DB

**目標：** 同一套 Python 可寫 SQLite（本機）或 Postgres（雲端）。

**工作項目：**

1. Supabase 專案（Free tier）建表（mirror `stock_db.py` schema + 建議 `sync_runs`）
2. 修改 `stock_db.py`：`DATABASE_URL` 存在 → psycopg2；否則 → SQLite
3. 本機測試：`DATABASE_URL=postgresql://... python sync_etf_holdings.py`
4. 新增 `pull_from_supabase.py`（雲端 → 本機 SQLite 增量 pull）

**Supabase 建表 SQL：** 見本文件 [附錄 A](#附錄-a-supabase-建表-sql)。

**依賴：** `requirements.txt` 加 `psycopg2-binary`（或 CI 單獨安裝）。

---

### Phase 2：GitHub Actions

**目標：** push repo 後可手動 / 被 n8n 觸發跑 sync。

**工作項目：**

1. Private GitHub repo，push 專案（`.env` / `data/` 已在 `.gitignore`）
2. Repository Secrets：
   - `SUPABASE_DATABASE_URL`
   - `TEJ_API_KEY`
   - `FINMIND_TOKEN`（選填）
3. 新增 `.github/workflows/daily_sync.yml`（見 [附錄 B](#附錄-b-github-actions-workflow-草稿)）
4. GitHub 網頁 **Run workflow** 驗證 → Supabase 有資料
5. **先不開** workflow 內 `schedule` cron（避免與 n8n 重複；Phase 3 再決定是否加 backup）

---

### Phase 3：n8n Cloud 排程

**目標：** 週一至五 20:30 / 22:00 TST 自動觸發 GA。

**工作項目：**

1. GitHub PAT（repo Actions 權限）存入 n8n Credentials
2. n8n Workflow A：Schedule `Asia/Taipei` 週一至五 20:30 → HTTP POST `workflow_dispatch`
3. n8n Workflow B：22:00 retry（或同一 workflow 兩個 Schedule）
4. 可選：查 GA run 狀態 → 成功/失敗 Line 通知
5. 停用本機 crontab（若曾設定）；Mac 睡眠不再影響 sync

**n8n HTTP 觸發 GA：**

```
POST https://api.github.com/repos/{owner}/{repo}/actions/workflows/daily_sync.yml/dispatches
Authorization: Bearer <GitHub PAT>
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28

Body:
{
  "ref": "main",
  "inputs": { "mode": "primary" }
}
```

**n8n 用量：** ~44 executions/月（2 次/工作日），遠低於 Cloud 方案上限。

---

### Phase 4：本機改 pull 模式（可選）

**目標：** 雲端為正庫；本機分析前 pull。

```bash
# .env 僅本機 pull 用（不設 DATABASE_URL，避免誤寫雲端）
SUPABASE_DATABASE_URL=postgresql://...

.venv/bin/python pull_from_supabase.py
.venv/bin/python sync_etf_holdings.py --changes
```

---

## 時區對照

| 台灣 (TST) | UTC | 用途 |
|------------|-----|------|
| 20:30 週一至五 | `30 12 * * 1-5` | Primary sync |
| 22:00 週一至五 | `0 14 * * 1-5` | Retry |

n8n Schedule 用 **`Asia/Taipei`**。GitHub Actions `schedule` 僅 UTC。

---

## 費用估算

| 項目 | 月費 |
|------|------|
| n8n Cloud | 已有 |
| GitHub Actions | $0（private repo 用量內） |
| Supabase Free | $0 |
| Render | 不用 |
| **合計增量** | **$0** |

Supabase Pro（+$25）僅在需要 7 天 backup、絕不 pause 時再考慮。

---

## 方案取捨（紀錄）

| 方案 | 決策 |
|------|------|
| Mac crontab + 喚醒 | 暫不採用（睡眠不可靠；Phase 3 改 n8n） |
| Render Cron ~$1 | 不採用（已有 n8n + GA $0） |
| Render Web $7 | 不採用 |
| n8n → GA → Supabase | **採用**（Phase 3 啟用） |
| 全 n8n HTTP 節點、無 Python | 不採用（EZMoney 解析適合 Python） |

---

## 安全

- API key、DB 連線 **只放** `.env`（本機）、GitHub Secrets、n8n Credentials
- **勿 commit** `.env`；repo 用 **Private**
- GitHub PAT 權限最小化（僅目標 repo Actions）

---

## 相關檔案（現有 + 待建）

| 檔案 | 狀態 |
|------|------|
| `query_stock_prices.py` | ✅ 已有 |
| `sync_etf_holdings.py` | ✅ 已有 |
| `stock_db.py` | ✅ SQLite；Phase 1 加 Postgres |
| `scripts/daily_sync.sh` | ✅ 本機 orchestrator |
| `pull_from_supabase.py` | ⬜ Phase 1 |
| `.github/workflows/daily_sync.yml` | ⬜ Phase 2 |
| Supabase SQL migration | ⬜ Phase 1 |

---

## 附錄 A：Supabase 建表 SQL

```sql
CREATE TABLE IF NOT EXISTS daily_bars (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open DOUBLE PRECISION,
    high DOUBLE PRECISION,
    low DOUBLE PRECISION,
    close DOUBLE PRECISION NOT NULL,
    volume BIGINT,
    spread DOUBLE PRECISION,
    source TEXT NOT NULL DEFAULT 'finmind',
    synced_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (code, date, source)
);

CREATE TABLE IF NOT EXISTS latest_quotes (
    code TEXT NOT NULL,
    name TEXT,
    market TEXT,
    date TEXT,
    close DOUBLE PRECISION,
    change DOUBLE PRECISION,
    change_pct DOUBLE PRECISION,
    volume BIGINT,
    source TEXT NOT NULL,
    queried_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (code, source)
);

CREATE TABLE IF NOT EXISTS etf_holdings_meta (
    etf_code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    nav DOUBLE PRECISION,
    holding_count INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'ezmoney',
    source_edit_at TEXT,
    synced_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (etf_code, snapshot_date)
);

CREATE TABLE IF NOT EXISTS etf_holdings (
    etf_code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT,
    shares DOUBLE PRECISION NOT NULL,
    weight_pct DOUBLE PRECISION,
    amount DOUBLE PRECISION,
    source TEXT NOT NULL DEFAULT 'ezmoney',
    source_edit_at TEXT,
    synced_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (etf_code, snapshot_date, stock_id)
);

CREATE INDEX IF NOT EXISTS idx_etf_holdings_date
    ON etf_holdings (etf_code, snapshot_date);

CREATE TABLE IF NOT EXISTS sync_runs (
    id BIGSERIAL PRIMARY KEY,
    job_name TEXT NOT NULL,
    mode TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    message TEXT,
    rows_written INTEGER
);
```

---

## 附錄 B：GitHub Actions workflow 草稿

路徑：`.github/workflows/daily_sync.yml`

```yaml
name: Daily Market + ETF Holdings Sync

on:
  workflow_dispatch:
    inputs:
      mode:
        description: primary or retry
        required: false
        default: primary
  # Phase 3 可選 backup（與 n8n 擇一或加 idempotent skip）：
  # schedule:
  #   - cron: '30 12 * * 1-5'
  #   - cron: '0 14 * * 1-5'

jobs:
  sync:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip
          cache-dependency-path: requirements.txt

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install psycopg2-binary

      - name: Hybrid market sync
        env:
          DATABASE_URL: ${{ secrets.SUPABASE_DATABASE_URL }}
          TEJ_API_KEY: ${{ secrets.TEJ_API_KEY }}
          FINMIND_TOKEN: ${{ secrets.FINMIND_TOKEN }}
        run: |
          python query_stock_prices.py \
            --sync-db --sync-mode hybrid --skip-watchlist \
            --benchmark-codes IX0001,IR0002 \
            --etf-codes 00981A,00403A,009816,00988A,00407A \
            --history-days 90

      - name: ETF signal sync
        env:
          DATABASE_URL: ${{ secrets.SUPABASE_DATABASE_URL }}
          FINMIND_TOKEN: ${{ secrets.FINMIND_TOKEN }}
        run: |
          python sync_etf_signal.py \
            --etf-codes 00981A,00403A,009816,00988A,00407A --lookback-days 14

      - name: ETF holdings sync
        env:
          DATABASE_URL: ${{ secrets.SUPABASE_DATABASE_URL }}
        run: |
          python sync_etf_holdings.py --etf-codes 00981A,00403A,00988A --source ezmoney
          python sync_etf_holdings.py --etf-codes 009816,00407A --source kgifund
```

---

## 附錄 C：Phase 0 手動驗證 checklist

```bash
# 1) Schema / 編譯
python -m py_compile query_stock_prices.py sync_etf_holdings.py sync_etf_signal.py stock_db.py

# 2) 完整 daily sync
scripts/daily_sync.sh

# 3) SQLite 摘要
sqlite3 data/stocks.db "SELECT code, MAX(date), COUNT(*) FROM daily_bars GROUP BY code;"
sqlite3 data/stocks.db "SELECT etf_code, source, MAX(snapshot_date), MAX(holding_count) FROM etf_holdings_meta GROUP BY etf_code;"

# 4) 持股 changes（需 ≥2 snapshot 日）
.venv/bin/python sync_etf_holdings.py --etf-codes 00981A,00403A,00988A,009816,00407A --changes
```

---

*最後更新：2026-06-03*
