---
name: hybrid-market-data-sync
description: Sync Taiwan ETF research data (5 ETFs) to SQLite using TEJ daily bars + index benchmarks, FinMind institutional signals, EZMoney holdings (統一 3 檔), and KGIFund holdings (凱基 009816/00407A). Use when running scripts/daily_sync.sh, ETF每日同步.command, query_stock_prices.py --skip-watchlist, sync_etf_holdings.py, sync_etf_signal.py, or checking TEJ quota.
disable-model-invocation: true
---

# Hybrid Market Data Sync (TEJ-first, 5 ETFs)

## Purpose

Run a **TEJ-first** daily sync for multi-ETF research into `data/stocks.db`:

| Layer | Source | Target table | Notes |
|-------|--------|--------------|-------|
| ETF 日線 | **TEJ** `TWN/EWPRCD` | `daily_bars` | 5 codes via `--etf-codes`; FinMind fallback if TEJ empty |
| 指數基準 | **TEJ** `TWN/EWIPRCD` | `daily_bars` | `idx_id=IX0001,IR0002` (**not** `coid`) |
| 三大法人 + close | **FinMind** | `etf_daily_signal_snapshot` | 14-day lookback upsert |
| 持股（統一） | **EZMoney** | `etf_holdings` | 00981A / 00403A / 00988A |
| 持股（凱基） | **KGIFund 官網** | `etf_holdings` | 009816=`J023`; 00407A fundID TBD until listed |
| TEJ 持股回溯 | 加購 `AETINV` | — | **不採用**（PDB003 未開通） |

**Not in daily flow (legacy / optional):**

| Layer | Source | Table | Status |
|-------|--------|-------|--------|
| WATCHLIST 20 檔 | FinMind | `daily_bars` | Removed from DB; `--skip-watchlist` in daily |
| 最新價對照 | Yahoo + FinMind | `latest_quotes` | Schema kept; daily no longer writes |

Yahoo index fallback in `query_stock_prices.py` is for **API/network errors only**.

## ETF universe (daily_sync.sh constants)

```bash
ETF_CODES="00981A,00403A,009816,00988A,00407A"      # 日線 + 法人
ETF_CODES_EZMONEY="00981A,00403A,00988A"             # 持股
ETF_CODES_KGIFUND="009816,00407A"                    # 持股
```

| ETF | 日線/法人 | 持股來源 | fundCode / fundID |
|-----|-----------|----------|-------------------|
| 00981A | ✅ | EZMoney | `49YTW` |
| 00403A | ✅ | EZMoney | `63YTW` |
| 00988A | ✅ | EZMoney | `61YTW` |
| 009816 | ✅ | KGIFund | `J023` |
| 00407A | ✅（掛牌前 warn skip） | KGIFund | fundID 掛牌後填入 `KGIFUND_FUND_MAP` |

**00407A：** 募集/掛牌前 FinMind 無日線/法人、凱基無持股 → log `SKIP`/`WARN`，**不 fail 整包**。

## Current phase: local manual (Phase 0)

**Now:** double-click desktop **`ETF每日同步.command`** or run `scripts/daily_sync.sh` → SQLite `data/stocks.db`. **No** crontab / cloud yet.

```bash
cd "/Users/jackm4/Desktop/股票研究"
scripts/daily_sync.sh
# or desktop: ~/Desktop/ETF每日同步.command
```

**Future cloud:** n8n → GitHub Actions → Supabase — see [`docs/cloud-sync-plan.md`](../../../docs/cloud-sync-plan.md).

## daily_sync.sh — four steps

| Step | Script | Writes |
|------|--------|--------|
| 1 | `query_stock_prices.py --skip-watchlist --etf-codes … --history-days 90` | `daily_bars` |
| 2 | `sync_etf_signal.py --etf-codes … --lookback-days 14` | `etf_daily_signal_snapshot` |
| 3a | `sync_etf_holdings.py --etf-codes EZMONEY --source ezmoney` | `etf_holdings` |
| 3b | `sync_etf_holdings.py --etf-codes KGIFUND --source kgifund` | `etf_holdings` |
| 4 | `--changes` on all 5 holdings codes | log only (≥2 snapshot dates) |

Flags: `--market-only` | `--holdings-only` | `--retry` (same day log append).

Logs: `logs/daily_sync_YYYYMMDD.log` (gitignored).

## Holdings rules (critical)

- **漏跑一天 = 少一個 snapshot 日，無法事後補**（官網只給最新一版）。
- 加減碼用 **`shares` 差分**，不是 `weight_pct`（`compute_etf_holdings_changes()` in `stock_db.py`）。
- 官網未更新 → `Skipped write: unchanged snapshot`（正常）。
- KGIFund parser: HTML table, dedupe by `stock_id`, skip TX futures; `amount=NULL`; `verify=False` for SSL on `kgifund.com.tw`.

### EZMoney fundCodes

| ETF | fundCode | ~rows |
|-----|----------|-------|
| 00981A | 49YTW | 51 |
| 00403A | 63YTW | 50 |
| 00988A | 61YTW | 51 |

### KGIFund

- URL: `https://www.kgifund.com.tw/Fund/Detail?fundID={id}`
- 009816 → `J023`

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

**Rules:** date-filter always; indices use `idx_id`, ETFs use `coid`. 5 ETF + 2 indices ≪ quota.

| Code | Meaning |
|------|---------|
| `LMT02` | Row limit — narrow date range |
| `PDB003` | Table not licensed — do not pursue AETINV for now |
| `PARAMERR` | Wrong filter — `idx_id` vs `coid` |

## Preconditions

- Project root: `/Users/jackm4/Desktop/股票研究`
- Python: `.venv/bin/python`
- `.env`: `TEJ_API_KEY` (required), `FINMIND_TOKEN` (optional, ETF 法人)
- Proxy: `daily_sync.sh` unsets `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`

## Command templates

### Full daily sync (recommended)

```bash
scripts/daily_sync.sh
```

### Market only

```bash
.venv/bin/python query_stock_prices.py \
  --sync-db --sync-mode hybrid --skip-watchlist \
  --benchmark-codes IX0001,IR0002 \
  --etf-codes 00981A,00403A,009816,00988A,00407A \
  --history-days 90
```

### Holdings dry-run

```bash
.venv/bin/python sync_etf_holdings.py --etf-codes 00981A,00403A,00988A --source ezmoney --dry-run
.venv/bin/python sync_etf_holdings.py --etf-code 009816 --source kgifund --dry-run
```

### Changes (needs ≥2 snapshot dates per ETF)

```bash
.venv/bin/python sync_etf_holdings.py \
  --etf-codes 00981A,00403A,00988A,009816,00407A --changes
```

### Signal dry-run

```bash
.venv/bin/python sync_etf_signal.py \
  --etf-codes 00981A,00403A,009816,00988A,00407A --lookback-days 14 --dry-run
```

## Validation checklist

- [ ] `daily_bars`: 5 ETF + IX0001 + IR0002 (`source=tej` where available)
- [ ] `etf_daily_signal_snapshot`: 4 listed ETFs have rows; 00407A may WARN until listed
- [ ] `etf_holdings_meta`: 00981A/00403A/00988A (`ezmoney`), 009816 (`kgifund`)
- [ ] `--changes` works after ≥2 trading days of holdings sync
- [ ] `logs/daily_sync_YYYYMMDD.log` exit=0
- [ ] `python -m py_compile query_stock_prices.py sync_etf_holdings.py sync_etf_signal.py stock_db.py`

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `PARAMERR` on index | Use `idx_id` on `EWIPRCD` |
| EZMoney empty | Unset proxy; retry evening |
| KGIFund SSL error | Expected; script uses `verify=False` for that host |
| `SKIP 00407A` | Not listed yet — normal |
| `--changes` needs 2 dates | Run daily on consecutive trading days |
| NAV ≠ close | NAV in `etf_holdings_meta`; close in `daily_bars` / signal |

## Related files

- `docs/cloud-sync-plan.md` — cloud roadmap (Phase 0 = local now)
- `scripts/daily_sync.sh` — orchestrator
- `scripts/ETF每日同步.command` — desktop entry
- `query_stock_prices.py` — hybrid TEJ + `--etf-codes` + `--skip-watchlist`
- `sync_etf_holdings.py` — EZMoney + KGIFund, `--etf-codes`, `--source`
- `sync_etf_signal.py` — FinMind, `--etf-codes`
- `stock_db.py` — schema + upsert helpers
- `data/stocks.db` — local cache
