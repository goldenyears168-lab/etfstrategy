# H4 · Premarket 五檔 depth → first-30m direction

Research only · **insufficient_data** · not Order / not Live / not `strategy.yaml`

Parent: `intraday-direction-thermometer` · Hypothesis: premarket full 5-level imbalance / depth (from `fubon_premarket_quote`) predicts **09:00–09:30** open direction (return or OR break) with OOS hit ≥55% · n≥200 · better than open-gap alone.

## Verdict: **insufficient_data**

Cannot test H4. Book research DB has **0** usable premarket 5-level stock-days; the only indirect artifact is **3** stock×days of trial-price accuracy (no depth features). Far below the pre-registered OOS gate **n≥200**.

**Wire to Live morning panel?** **No** — not until depth history accumulates and an OOS test clears the gate.

## Data inventory（PIT sources）

| Source | Path / table | Status on Book `data/stocks.db` | Premarket 5-level? | Usable for H4? |
|---|---|---|---|---|
| Fubon / Fugle quote collect | `fubon_premarket_quote_snapshot` | **rows = 0** | Schema yes (`bid_prices/sizes`, `ask_prices/sizes` JSON · 5 levels) | **No** (empty) |
| Trial-price accuracy export | `reports/research/fubon_premarket_quote_accuracy.csv` | **3 rows** · `2026-07-23` · `0050,2330,6451` | **No** (trial vs open only) | **No** (wrong features; n=3) |
| FinMind auction (retired) | `pre_market_auction_snapshot` | 2855 rows · **1 date** `2026-07-21` | Almost never: L1-only post-open; 08:30 rows are non-equity / null / zero | **No** (known dead feed; collector disabled 2026-07-22) |
| Labels (first 30m) | `stock_kbar_1m` | 2022-02-08 → 2026-07-22 · 287 stocks · 743 dates | n/a | Ready **if** features existed |

### Collector design (what *would* be stored)

- Script: `scripts/research/collect_fubon_premarket_quote.py`
- Storage: `src/stock_db/fubon_premarket_quote.py` → table `fubon_premarket_quote_snapshot`
- Window: 08:29–09:01 · poll ~5s · full `bids`/`asks` price+size + `lastTrial`
- Default universe: holdings ∪ `{0050,2317,2330,2454}` （~O(10) symbols/day, not market-wide）
- Launchd: `com.jackm4.etf.fubon-premarket-quote-collect`（manual install on mini only; **not** in `install-launchd.sh` LABELS）
- Analyze script only checks **trial→open** error, not depth→30m: `scripts/research/analyze_fubon_premarket_quote.py`

### FinMind auction (why not a substitute)

- Docstring in `fubon_premarket_quote.py`: FinMind tick snapshot has **no real 08:30–09:00 signal** for normal stocks; retired 2026-07-22.
- Empirically on Book DB: pre-09:00 rows with both bid+ask = **2** stock-days (junk codes / zeros); 5-level depth count among sampled rows = **0**.

## Pre-registered test (not run — blocked)

If / when `fubon_premarket_quote_snapshot` has history:

**Features** (last poll with `poll_ts` time `< 09:00:00`, PIT):

- `imb_l1 = (bid_size[0] - ask_size[0]) / (bid_size[0] + ask_size[0])`
- `imb_sum5 = (Σ bid_size[:5] - Σ ask_size[:5]) / (Σ bid + Σ ask)`
- `spread_bps = (ask[0] - bid[0]) / mid * 1e4`
- Optional: trial vs prior close gap (baseline comparator = **open-gap alone**)

**Label** (first 30m):

- Primary: `sign(close_09:30 / open_09:00 - 1)` from `stock_kbar_1m`（or 5m OR high/low break direction）
- Exclude flat `|ret| < ε` from directed hit

**Split / gate**:

- IS / OOS by calendar (e.g. first 60% dates IS)
- Claim only if **OOS directed hit ≥55%** and **OOS n≥200** and **beats open-gap baseline** on same rows

## Gap quantification

| Metric | Value | vs gate |
|---|---:|---|
| Fubon depth stock×days on Book | **0** | need ≫200 for OOS alone |
| Accuracy CSV stock×days (no depth) | **3** | irrelevant to H4 features |
| FinMind usable premarket 5-level stock×days | **0** | — |
| Implied calendar at ~10 symbols/day for **total** n≈400 (IS+OOS) | **~40 trading days** of continuous collect + Book sync | not started on Book |
| Implied for OOS n≥200 only @10/day | **≥20 trading days** after a frozen IS cut | — |

Even a successful mini collect on **2026-07-23** (evidenced by accuracy CSV for 3 names) does **not** appear in Book `fubon_premarket_quote_snapshot` (replica empty). Depth history is not research-available here.

## Live morning panel

| Question | Answer |
|---|---|
| Worth wiring depth imb into Live now? | **No** |
| Why | No OOS evidence; schema/collector exist for **research accumulate** only |
| What to do instead | Keep mini collect running; periodically sync / copy `fubon_premarket_quote_snapshot` (or a feature parquet) to Book; re-run H4 when OOS n can clear 200 |

## Paths / commit

| Item | Path |
|---|---|
| This report | `reports/research/intraday_direction_thermometer/H4_PREMARKET_DEPTH_30M.md` |
| Schema | `src/stock_db/_schema.py` · `fubon_premarket_quote_snapshot` |
| Upsert API | `src/stock_db/fubon_premarket_quote.py` |
| Collect / analyze | `scripts/research/collect_fubon_premarket_quote.py`, `analyze_fubon_premarket_quote.py` |
| Only non-empty artifact | `reports/research/fubon_premarket_quote_accuracy.csv` |
| Workspace HEAD (report authored against) | `aa24b43` |

Report file is **uncommitted** at authorship time (research VFP only).
