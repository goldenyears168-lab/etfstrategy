# H1 · Fubon L1 size imbalance vs short mom

Research only · **未採納** · not Order / not `strategy.yaml` · 2026-07-24  
Parent: `intraday-direction-thermometer`  
Agent: Research H1 (Book)

## Verdict

**Hypothesis status: `insufficient_data`**

Cannot confirm or reject H1 under the stated gate (OOS directed hit ≥55% · **n≥300** · vs mom-only). Book/replica have **no multi-day continuous-session L1 size history** suitable for calendar IS/OOS. Do **not** treat the one-day exploratory peek below as OOS evidence.

- Best confirmatory OOS number: **n/a**
- Commit: **none** (report only · no reusable code)

## 1. Hypothesis (locked)

**H1:** At snapshot time, Fubon top-of-book size imbalance

\[
\mathrm{imb}_1 = \frac{\mathrm{buy1\_size} - \mathrm{sell1\_size}}{\mathrm{buy1\_size} + \mathrm{sell1\_size}}
\]

predicts **next 1–2 minute** mid return better than **price short-mom alone** (`mom_1bar` / `mom_2bar`).

Success gate (confirmatory): OOS directed hit ≥ **55%** with pool **n≥300**, and beats mom-only (and preferably always-long). Fail honestly otherwise.

Caveats (from brief): Live TA poll ~15s → state latency; do **not** conflate with afternoon fade champion `fade_idx_or_inside` (~30m · different horizon).

## 2. Data inventory (Book + replica)

| Source | What it stores | Coverage (queried 2026-07-24) | Usable for H1? |
|--------|----------------|----------------------------------|----------------|
| `fubon_premarket_quote_snapshot` | Full 5-level `bid_sizes` / `ask_sizes` (JSON) | **Book `data/stocks.db`:** 0 rows. **Replica:** 7,329 rows · **1 day** `2026-07-23` · 4 sids | **Partial only** — see §2.1 |
| `pre_market_auction_snapshot` | Best-1 FinMind tick volumes (`buy_volume`/`sell_volume` as 1-element arrays); market orderbook aggregate | Book: 2,855 · mostly `2026-07-21` close-cache; replica: 7,512 · 2 days | **No** — not continuous session L1×1–2m path; not Fubon 五檔 |
| `intraday_1m_bars.order_imbalance_1` | Column exists | 8 Yahoo stub rows · all `0.0` | **No** |
| `intraday_signals` | Column exists | 0 rows | **No** |
| `stock_kbar_1m` | OHLC+volume only | ~9.15M bars · 158 sids · through ~2026-07-15 | Price/mom baseline only · **no size** |
| Live TA / `ops.live_ta` | Fubon `intraday.quote` → **L1 bid/ask price only** | Observe poll · **size discarded** in `ops_live_ta` | **No history of size** |
| FinMind `TaiwanStockPriceMinuteBidAsk` | Historical minute bid/ask | **Dead** (v3 404; not in v4 enum) — see `collect_pre_market_auction_snapshot.py` | **No** |
| Parquet / other quote archives | — | None found under `data/` | **No** |

### 2.1 Replica Fubon snapshot detail (`2026-07-23`)

| stock_id | Premarket polls | Continuous-session polls (with sizes) | Notes |
|----------|-----------------|----------------------------------------|-------|
| 0050, 2330, 6451 | ~365 each (~08:29–09:00) | ~5 each (just after open) | Auction-oriented collect |
| **2492** | 0 | **6,219** · ~09:51–13:20 · **~2s gap** · full 5-level sizes | Accidental / extended poll of one name |

So: schema + collector **can** persist L1 size during the session, but research DB has **one stock-day** of continuous L1 size — far below multi-day OOS `n≥300`.

## 3. Metric / protocol (what we would run if data existed)

| Item | Spec |
|------|------|
| Feature | \(\mathrm{imb}_1\) from Fubon quote L1 sizes at poll \(t\) (PIT: sizes known at \(t\)) |
| Price | L1 mid \((\mathrm{bid1}+\mathrm{ask1})/2\) (prefer over stale `last_trade` when prints lag) |
| Label | \(r_{t\to t+1\mathrm{m}}\), \(r_{t\to t+2\mathrm{m}}\) on mid (or 1m bar close if aligning to `stock_kbar_1m`) |
| Directed hit | \(\mathrm{sign}(\mathrm{imb}_1)=\mathrm{sign}(r)\) when \(\lvert r\rvert \ge \varepsilon\) (e.g. 1 tick / 1e−6 mid) and \(\mathrm{imb}_1 \ne 0\); optional \(\lvert\mathrm{imb}_1\rvert>\tau\) |
| Baselines | (1) `sign(mom_1bar)` / `sign(mom_2bar)` · (2) always-long |
| IS/OOS | **Calendar** split by `trade_date` (e.g. IS ≤ cutoff · OOS > cutoff); **no** same-day AM/PM pseudo-OOS for claims |
| Sampling | One observation per stock per minute (first poll in minute) to limit 2s autocorrelation; report effective n and cluster by day |
| Latency note | Live poll ~15s → optional robustness: shift feature by +1 poll / +15s |

## 4. Exploratory peek only (not OOS · not a claim)

**Universe:** `2492` · `2026-07-23` · minute-spaced mid series · n_minutes≈210 · replica `fubon_premarket_quote_snapshot`.

| Rule | h=1m hit (n) | h=2m hit (n) |
|------|--------------|--------------|
| \(\mathrm{sign}(\mathrm{imb}_1)\) · \(\lvert\mathrm{imb}\rvert>0.05\) | 54.8% (124) | 51.8% (139) |
| `mom_1bar` | **68.0% (100)** | 57.1% (105) |
| `mom_2bar` | 58.9% (107) | 51.7% (118) |
| always-long | 53.4% (131) | 52.7% (148) |

Same-day 11:30 split “OOS” (invalid for gate): imb h1 ≈61% at **n=59 ≪ 300**; **mom_1 still higher** (~79% · n=42).

**Interpretation:** On this single day, imbalance does **not** beat short mom. Sample is too small / non-independent across days to **reject** H1 formally either → remain **`insufficient_data`**.

## 5. Data gap & collection design (minimum for a real test)

To reopen H1 honestly:

1. **Persist continuous-session Fubon quotes with sizes** (reuse `fubon_premarket_quote_snapshot` schema or a dedicated `fubon_intraday_quote_snapshot`) for a fixed watch universe (e.g. Live TA holdings + 0050/2330).
2. **Cadence:** ≤15s poll (match Live TA) during 09:00–13:25; store `poll_ts`, L1–L5 price+size, last trade, mid.
3. **Duration:** ≥ **10–15 trading days** (preferably ≥20) so minute-spaced directed events can clear **OOS n≥300** after a calendar holdout (e.g. last 5–7 days OOS).
4. **Join:** optional align to `stock_kbar_1m` for PIT mom baselines; keep quote mid as primary label source when prints are sparse.
5. **Do not** use Live TA Supabase row as history (latest-state only); do not graduate to Order from this track.
6. **Mini vs Book:** production collect on mini if launchd; Book research reads replica/rsync — no dual Order.

Until that history exists, H1 stays **blocked on data**, not on alpha.

## 6. Relation to other thermometer work

- Fade / E1–E5 / `fade_idx_or_inside` ≈ **30m** directed hit — **different horizon**; irrelevant as H1 baseline.
- Live TA today: L1 **price** only (`ops_live_ta` ignores 五檔 size) — consistent with why H1 cannot be backtested from observe logs.
