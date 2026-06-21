---
name: hybrid-market-data-sync
description: Sync Taiwan ETF research data (7 holdings ETFs) to local SQLite using TEJ, FinMind, Yahoo, and fund-site holdings APIs. Use when running scripts/daily_sync.sh, weekly_sync.sh, src/sync_*.py, src/query_stock_prices.py, or checking TEJ quota. Pair with scheme-c-local-ops for schedule profiles.
disable-model-invocation: true
---

# Hybrid Market Data Sync (TEJ-first, 7 holdings ETFs)

## Purpose

Run a **TEJ-first** daily sync for multi-ETF research into `data/stocks.db`:

| Layer | Source | Target table | Notes |
|-------|--------|--------------|-------|
| ETF 日線 | **TEJ** `TWN/EWPRCD` → FinMind `TaiwanStockPrice` | `daily_bars` | 6 codes via `--etf-codes`; TEJ 失敗/空資料自動 fallback |
| 指數基準 | **TEJ** `TWN/EWIPRCD` | `daily_bars` | `idx_id=IX0001,IR0002` (**not** `coid`) |
| 三大法人 + close | **FinMind** | `etf_daily_signal_snapshot` | `ENABLE_FINMIND_SIGNAL=1` · 14-day lookback |
| 科技風險三層 | **Yahoo + FinMind** | `tech_risk_daily_snapshot` + `daily_bars` | TSM ADR / ^SOX / TX·TE gap（`sync_tech_risk_context.py`） |
| 早盤期貨 gap | **FinMind Sponsor** | `morning_risk_snapshot` | TX/TE 即時（`sync_morning_futures.py` · ①） |
| 持股（統一） | **EZMoney** | `etf_holdings` | 00981A / 00403A |
| 持股（凱基） | **KGIFund 官網** | `etf_holdings` | 009816=`J023`; 00407A fundID TBD until listed |
| 持股（群益） | **CapitalFund CFWeb** | `etf_holdings` | POST `buyback`; fundId 399/500 |
| 持股（野村） | **Nomura ETFAPI** | `etf_holdings` | POST `GetFundAssets`; FundID=00980A |
| 成分股價+法人 | **FinMind** | `stock_daily_bars`、`stock_institutional_daily` | `RUN_STOCK_MARKET_SYNC=1` |
| 融資/借券/當沖 | **FinMind** | `stock_margin_daily` 等 | `RUN_CHIP_SYNC=1` → 籌碼 Gate |
| Sponsor 分點/鉅額 | **FinMind Sponsor** | `stock_branch_daily`、`stock_block_trade` | 週日 · `RUN_SPONSOR_CHIP_SYNC=1` |

**Not in daily flow (manual / weekly):**

| Layer | Source | Table | Status |
|-------|--------|-------|--------|
| 上市櫃 Beta | Yahoo vs ^TWII | `stock_beta` | `weekly_sync.sh` |
| 基本面 + consensus proxy | FinMind | `stock_fundamental`、`stock_consensus` | `sync_fundamentals.py`（週日） |

Yahoo index fallback in `query_stock_prices.py` is for **API/network errors only**.

## ETF universe (daily_sync.sh constants)

```bash
ETF_CODES="00981A,00403A,009816,00980A,00982A,00992A"   # 日線 TEJ
ETF_CODES_EZMONEY="00981A,00403A"
ETF_CODES_KGIFUND="009816,00407A"
ETF_CODES_CAPITALFUND="00982A,00992A"
ETF_CODES_NOMURA="00980A"
ETF_CODES_HOLDINGS="${ETF_CODES_EZMONEY},${ETF_CODES_KGIFUND},${ETF_CODES_CAPITALFUND},${ETF_CODES_NOMURA}"
```

| ETF | 日線/法人 | 持股來源 | fundCode / fundID |
|-----|-----------|----------|-------------------|
| 00981A | ✅ | EZMoney | `49YTW` |
| 00403A | ✅ | EZMoney | `63YTW` |
| 009816 | ✅ | KGIFund | `J023` |
| 00407A | ✅（掛牌前 warn skip） | KGIFund | fundID 掛牌後填入 `KGIFUND_FUND_MAP` |
| 00980A | ✅ | Nomura ETFAPI | `FundID=00980A` |
| 00982A | ✅ | CapitalFund CFWeb | `fundId=399` |
| 00992A | ✅ | CapitalFund CFWeb | `fundId=500` |

**00407A：** 募集/掛牌前 FinMind 無日線/法人、凱基無持股 → log `SKIP`/`WARN`，**不 fail 整包**。

## Local only · Scheme C

**Storage:** `data/stocks.db`（SQLite）。**Code:** `src/`；`scripts/daily_sync.sh` 會 `export PYTHONPATH=src`。

| Profile | Command | Steps |
|---------|---------|-------|
| ① `morning-risk` | `--market-only` | TEJ 日線、tech_risk、morning_futures、opt FinMind ETF 法人 |
| ② `evening-holdings` | `--holdings-only` | 四源持股 + chip/market sync + pipeline + analytics |
| ③ `weekly-deep` | `weekly_sync.sh` | Beta、基本面、batch 成分股/籌碼、Sponsor |
| 全量 | （無 flag） | ①+② |

```bash
cd "<project-root>"
scripts/daily_sync.sh --quiet
# 或 scripts/0830執行評估.command / 1630收盤雷達.command
```

詳見 skill **`scheme-c-local-ops`**（本地 SQLite；不做雲端 Supabase 同步）。

## daily_sync.sh — steps（`src/` 腳本）

| Step | Script | Writes |
|------|--------|--------|
| 1 | `src/query_stock_prices.py --sync-db …` | `daily_bars` |
| 1b | `ENABLE_FINMIND_SIGNAL=1` → `sync_etf_signal.py` | `etf_daily_signal_snapshot` |
| 1c | `sync_tech_risk_context.py --sync-db` | `tech_risk_daily_snapshot` |
| 1d | `sync_morning_futures.py --sync-db` | `morning_risk_snapshot` |
| 2a–d | `src/sync_etf_holdings.py --no-auto-changes …` | `etf_holdings` |
| 2b | `RUN_STOCK_MARKET_SYNC=1` → `sync_stock_market_daily.py` | 成分股價+法人 |
| 2c | `RUN_CHIP_SYNC=1` → `sync_stock_chip_daily.py` | 融資/借券/當沖 |
| 3 | `--changes --intent` + `sync_flow_events` | log + `flow_events` |
| 4 | `etf_daily_report.py --write-reports` | `reports/daily/etf-daily/daily_brief.md` |
| 5 | `regime_daily_brief.py --write-reports` | `reports/daily/regime/daily_brief.md` |

**已退役**：`pipeline_evening.py` · `RUN_SCORE_ENGINE=1` · `factor_ic.md` daily 鏈。

Flags: `--quiet` | `--market-only` | `--holdings-only` | `--retry`.

Logs: `logs/daily_sync_YYYYMMDD.log` (gitignored).

## weekly_sync.sh

| Step | Script |
|------|--------|
| Beta | `sync_stock_beta.py --sync-db` |
| 基本面 | `sync_fundamentals.py --sync-db`（含 `stock_consensus` FinMind proxy） |
| 成分股 batch | `sync_stock_market_daily.py`（90d） |
| 籌碼 batch | `sync_stock_chip_daily.py`（90d） |
| Sponsor | `sync_stock_sponsor_daily.py` Top N |

## Holdings rules (critical)

- **漏跑一天 = 少一個 snapshot 日，無法事後補**（官網只給最新一版）。
- 加減碼用 **`shares` 差分**，不是 `weight_pct`（`src/stock_db.py`）。
- 官網未更新 → `Skipped write: unchanged snapshot`（正常）。
- KGIFund / Nomura parser: `verify=False` for SSL on those hosts.
- 群益/野村解析後若台股檔數 **< 40** → 視為不完整，該次同步 **失敗**（避免前十大誤進共識）。

### EZMoney fundCodes

| ETF | fundCode | ~rows |
|-----|----------|-------|
| 00981A | 49YTW | 51 |
| 00403A | 63YTW | 50 |

### KGIFund

- URL: `https://www.kgifund.com.tw/Fund/Detail?fundID={id}`
- 009816 → `J023`

### CapitalFund (群益)

- POST `https://www.capitalfund.com.tw/CFWeb/api/etf/buyback`
- Body: `{"fundId":"<id>","date":null}`
- 00982A → `399`；00992A → `500`（完整持股約 45–55 檔）

### Nomura (野村)

- POST `https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundAssets`
- Body: `{"FundID":"00980A","SearchDate":null}`
- 解析 `Entries.Data.Table` 中 `TableTitle=股票`（約 44+ 檔）

## Data retention policy

| Data | Can backfill via API? | Daily strategy |
|------|----------------------|----------------|
| 日線 / 法人 / 籌碼 | Yes | 60–90d lookback upsert |
| 持股 snapshot | **No** | Must run every trading day |

## TEJ quota (斜槓方案)

```bash
curl --compressed "https://api.tej.com.tw/api/apiKeyInfo/${TEJ_API_KEY}"
```

| Item | Limit |
|------|-------|
| Daily API calls | 1,000 |
| Daily data rows | 500,000 |

**Rules:** date-filter always; indices use `idx_id`, ETFs use `coid`. 6 ETF + 2 indices ≪ quota.

**程式實際使用 TEJ 表**：`EWPRCD`（ETF 日線）、`EWIPRCD`（指數）。斜槓其餘表（EWIFINQ、EWSALE 等）**未接入**；基本面走 FinMind。

| Code | Meaning |
|------|---------|
| `LMT02` | Row limit — narrow date range |
| `PARAMERR` | Wrong filter — `idx_id` vs `coid` |

## Preconditions

- **cwd:** project root（`data/`、`logs/` 相對路徑）
- Python: `.venv/bin/python`；`PYTHONPATH=src`（`daily_sync.sh` 已設定）
- `.env`: `TEJ_API_KEY` (required), `FINMIND_TOKEN` (Sponsor 功能建議)
- Proxy: `daily_sync.sh` unsets `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`

## Command templates

```bash
cd "<project-root>"
export PYTHONPATH=src
scripts/daily_sync.sh --quiet

.venv/bin/python src/sync_etf_holdings.py --etf-codes 00982A --source capitalfund --dry-run
.venv/bin/python src/sync_etf_holdings.py --etf-codes 00981A,00403A,009816,00980A,00982A,00992A --changes --intent
```

## Validation checklist

- [ ] `daily_bars`: 6 ETF + IX0001 + IR0002
- [ ] `etf_holdings_meta`: holding_count ≥ 40（群益/野村）
- [ ] `RUN_CHIP_SYNC=1` 時 `stock_margin_daily` 有覆蓋
- [ ] `logs/daily_sync_YYYYMMDD.log` exit=0
- [ ] `python -m py_compile src/sync_etf_holdings.py src/stock_db.py`

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `PARAMERR` on index | Use `idx_id` on `EWIPRCD` |
| EZMoney empty | Unset proxy; retry evening |
| KGIFund / Nomura SSL error | Expected; script uses `verify=False` for those hosts |
| CapitalFund `< 40` stocks | API 異常或官網未更新；檢查 buyback 回應 |
| `SKIP 00407A` | Not listed yet — normal |
| `--changes` needs 2 dates | Run daily on consecutive trading days |
| FinMind 403 on ETF 法人 | `ENABLE_FINMIND_SIGNAL=0` |

## Related

- `scripts/daily_sync.sh`、`scripts/weekly_sync.sh`
- `src/sync_etf_holdings.py`、`src/sync_etf_signal.py`、`src/sync_stock_chip_daily.py`、`src/stock_db.py`
- `docs/PRD.md`（§5.7 架構、§10 **p6-tier** 評分）、`docs/daily-operations.md`
- Skills: `scheme-c-local-ops`、`etf-holdings-intent`
