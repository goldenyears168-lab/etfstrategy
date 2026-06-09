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
| 三大法人 + close | **FinMind** | `etf_daily_signal_snapshot` | 14-day lookback upsert |
| 科技風險三層 | **Yahoo + FinMind** | `tech_risk_daily_snapshot` + `daily_bars` | TSM ADR / ^SOX / TX·TE gap（`sync_tech_risk_context.py`） |
| 持股（統一） | **EZMoney** | `etf_holdings` | 00981A / 00403A |
| 持股（凱基） | **KGIFund 官網** | `etf_holdings` | 009816=`J023`; 00407A fundID TBD until listed |
| 持股（群益） | **CapitalFund CFWeb** | `etf_holdings` | POST `buyback`; fundId 399/500 |
| 持股（野村） | **Nomura ETFAPI** | `etf_holdings` | POST `GetFundAssets`; FundID=00980A |
| TEJ 持股回溯 | 加購 `AETINV` | — | **不採用**（PDB003 未開通） |

**Not in daily flow (manual / weekly):**

| Layer | Source | Table | Status |
|-------|--------|-------|--------|
| 上市櫃 Beta | Yahoo vs ^TWII | `stock_beta` | `src/sync_stock_beta.py`（③ 週日 `weekly_sync.sh`） |

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
| ① `morning-risk` | `--market-only` | TEJ 日線、tech_risk、opt FinMind 法人 |
| ② `evening-holdings` | `--holdings-only` | 四源持股 + `--changes --intent` |
| ③ `weekly-deep` | `weekly_sync.sh` | `sync_stock_beta.py` |
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
| 2a–d | `src/sync_etf_holdings.py --no-auto-changes …` | `etf_holdings` |
| 3 | `src/sync_tech_risk_context.py --sync-db` | `tech_risk_daily_snapshot` |
| 4 | `--changes --intent` | log only |
| opt | `ENABLE_FINMIND_SIGNAL=1` → `src/sync_etf_signal.py` | 需 FinMind 權限 |

Flags: `--quiet` | `--market-only` | `--holdings-only` | `--retry`.

Logs: `logs/daily_sync_YYYYMMDD.log` (gitignored).

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
| 日線 / 法人 | Yes | 90d + 14d lookback upsert |
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

| Code | Meaning |
|------|---------|
| `LMT02` | Row limit — narrow date range |
| `PDB003` | Table not licensed — do not pursue AETINV for now |
| `PARAMERR` | Wrong filter — `idx_id` vs `coid` |

## Preconditions

- **cwd:** project root（`data/`、`logs/` 相對路徑）
- Python: `.venv/bin/python`；`PYTHONPATH=src`（`daily_sync.sh` 已設定）
- `.env`: `TEJ_API_KEY` (required), `FINMIND_TOKEN` (optional)
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

## Related

- `scripts/daily_sync.sh`、`scripts/weekly_sync.sh`
- `src/sync_etf_holdings.py`、`src/sync_etf_signal.py`、`src/sync_tech_risk_context.py`、`src/stock_db.py`
- `docs/PRD.md`、`docs/architecture.md`
- Skills: `scheme-c-local-ops`、`etf-holdings-intent`
