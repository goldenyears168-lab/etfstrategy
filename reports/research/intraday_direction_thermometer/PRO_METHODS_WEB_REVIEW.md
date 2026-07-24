# Professional intraday methods · Web review

Research only · **未採納** · not Order / not `strategy.yaml` · 2026-07-24

Parent: `intraday-direction-thermometer` · Synthesis: [`SYNTHESIS_MULTI_SIGNAL_70.md`](./SYNTHESIS_MULTI_SIGNAL_70.md)  
Anchor: `fade_near_ext` OOS ~62%; partial champion `fade_idx_or_inside` ~70.7% (0050 proxy) / ~66.9% (IX0001 OOS sens.)

## 0. Scope

Re-searched **global day-trading + TW** sources on practices relevant to combining:

1. Opening range / first 30–60 min  
2. VWAP mean-reversion vs trend  
3. Fade at extremes / midday mean reversion  
4. Relative strength vs index  
5. Volume confirmation  
6. Institutional / order-flow proxies (vs EOD 三大法人)

**Not** a Live deploy brief; **not** Order graduation.

---

## 1. Pro practice distillate

### 1.1 Day-type first (regime fork) — the master rule

Pros treat **ORB breakout** and **VWAP / extreme fade** as **opposite games**. Same candle (e.g. poke above OR high) is continuation on a **trend / open-drive** day and a **trap / fade** on a **rotational** day.

| Day type | VWAP role | ORB state | Preferred play |
|----------|-----------|-----------|----------------|
| Trend / open drive | Support/resistance; hold one side | Clean break + hold | Follow breakout; **do not fade** |
| Rotation / range | Magnet; flat slope | Failed break / poke-and-fail | Fade extremes; target VWAP / opposite OR |
| Reversal | Role flip mid-session | Trap then opposite | Wait for VWAP flip / OR reclaim |
| Midday chop | Magnet; thin volume | Weak fake breaks | Fail-break fade or **no trade** |

Sources: [Comborb NQ ORB](https://comborb.com/nq-orb-strategy), [Comborb Context Engine](https://comborb.com/context-engine-indicator), [Chart Whisperer VWAP 2026](https://chartwhisperer.ca/blog/vwap-trading-guide), [CrossTrade VWAP reversion](https://crosstrade.io/learn/trading-strategies/vwap-reversion).

### 1.2 Opening range (ORB)

- Formalized by Toby Crabel (*Day Trading… Opening Range Breakout*, 1990); windows commonly **5 / 15 / 30 min**.
- **Breakout path:** candle **close** beyond ORH/ORL (not wick), ideally **RVOL ≥ 1.5–2×** time-of-day average, price on correct **VWAP** side, **RS vs index** aligned, optional **retest**.
- **Fade / failed-break path:** poke beyond OR then **close back inside**, preferably on **below-average volume** on the poke; stop just beyond failed extreme; magnets = OR mid / opposite side / session VWAP. Often **higher-probability than raw breakout** on rotational days ([Steady Turtle ORB](https://steady-turtle.com/knowledge/opening-range-breakout-strategy)).
- **ATR context:** OR width ≲ ~0.4–0.5× ATR(14) → weak / skip or expect chop; OR already ≳ ~0.7–0.75× daily ATR → exhaustion → prefer **mean reversion**, not chase breakout ([DXP refined ORB](https://www.dxpa.in/resources/opening-range-breakout-refined), Finexus ATR/RVOL notes).
- **Time:** ORB edge concentrated **morning**; many desks **skip post-noon** OR breaks ([GrandAlgo ORB](https://grandalgo.com/blog/opening-range-breakout-strategy), [Intraday Lab Nifty](https://intradaylab.com/blog/breakout-trading-nifty-what-actually-works)).

### 1.3 VWAP

- Institutional **execution benchmark**, not a crossover signal ([Chart Whisperer](https://chartwhisperer.ca/blog/vwap-trading-guide); TW: [richkpi VWAP](https://richkpi.com/vwap-app/), [自營家 Peter](https://individual-trader.blogspot.com/2025/04/20250429-vwap.html)).
- **Trend day:** pullback to VWAP (or 1σ) **with** trend; do **not** fade 2σ extensions while price rides the band.
- **Range day:** fade **~2σ** extensions with **rejection candle**; target VWAP; skip if ADX high / news / opening-hour range >> average ([CrossTrade](https://crosstrade.io/learn/trading-strategies/vwap-reversion)).
- **TW futures nuance:** night-session high/low + anchored / night-start VWAP as overnight context ([老墨 · 台指期](https://mofiinvestment.com/blog/taiex-futures-vwap-night-session/)).

### 1.4 Midday fade

- US lunch (≈11:00–14:00 ET) / TW analogue: volume dries → fake breaks; **A+** = failed breakout on **weak volume**, fade back into range ([DayTradingToolkit midday](https://daytradingtoolkit.com/strategies/traders-playbook-midday-chop)).
- Aligns with our `fade_near_ext` **midday_only** design — but pros add **day-type / OR-intact / RVOL-dry** gates, not raw extreme alone.

### 1.5 Relative strength vs index

- For **ORB longs:** stock outperforming index + index itself breaking OR same way; fighting the tape cuts win rate materially (~15pp cited in [DXP](https://www.dxpa.in/resources/opening-range-breakout-refined)).
- For **fade:** pros often fade when **index OR still intact** (rotation day proxy) — close to our `fade_idx_or_inside` — rather than using overnight RS or daily RRG as 30m predictors.
- **Avoid:** treating daily RRG / WMA as intraday direction (horizon mismatch).

### 1.6 Volume

- Breakouts need **above-average / RVOL** participation; low-vol breaks = classic fakeout ([ChartingLens ORB](https://chartinglens.com/blog/opening-range-breakout-strategy), [Intraday Lab](https://intradaylab.com/blog/breakout-trading-nifty-what-actually-works)).
- Fades prefer **dry-up / below-avg volume on the poke**; rejection bar + volume spike on the rejection itself is a common trigger ([Steady Turtle](https://steady-turtle.com/knowledge/opening-range-breakout-strategy), [CrossTrade](https://crosstrade.io/learn/trading-strategies/vwap-reversion)).
- Time-slot **RVOL** (vs same clock bar history) > raw “volume > SMA” for session microstructure.

### 1.7 Institutional / order-flow — what pros actually use

| Proxy | Used by pros for intraday? | Notes |
|-------|----------------------------|-------|
| Session VWAP / AVWAP | **Yes** | Fair-value / execution benchmark |
| RVOL, time-of-day volume | **Yes** | Participation / fakeout filter |
| CVD / delta / footprint / absorption | **Yes** (futures / tick feeds) | Aggression + passive defense at VWAP/OR |
| Prior day / night H-L, gap | **Yes** | Overnight positioning context |
| EOD 三大法人 / fund ownership | **No** (as live 30m trigger) | Too laggy; overnight bias only |

Sources: [United Daytraders CVD](https://united-daytraders.com/blog/delta-cvd-advanced-order-flow), [D&T Systems order flow](https://dtsystems.dev/blog/order-flow-trading-explained), [NinjaTrader footprint](https://ninjatrader.com/futures/blogs/footprint-charts-guide/).

### 1.8 Win-rate honesty (do not chase 80%+)

| Setup family | Realistic hit rate (cited) | Edge driver |
|--------------|---------------------------|-------------|
| Raw ORB breakout | ~40–52% | R:R + trend-day runners |
| ORB + RVOL / filters | ~52–65% | Selectivity |
| ORB retest | ~60–68% | Fewer trades |
| VWAP reversion (filtered) | ~55–65% | Close targets; ADX/news skip |
| Unfiltered reversion | ~45% | Trend days crush fades |

Sources: [The ORB Strategy Blog](https://theorbstrategy.com/blog/orb-strategy-win-rate-and-backtesting-basics/), [ChartingLens](https://chartinglens.com/blog/opening-range-breakout-strategy), [CrossTrade](https://crosstrade.io/learn/trading-strategies/vwap-reversion).

**Implication for our OOS≥70% goal:** 70% directed-hit is **above** typical published ORB/VWAP hit rates; achievable only with **narrow filters + short targets** (or metric that differs from full trade P&L). Expectancy / R:R still matter if we ever Order-graduate.

---

## 2. What they combine vs avoid

### Combine (confluence stack)

1. **Day type** (OR width, VWAP slope/side, failed vs clean break)  
2. **Setup family** matched to day type (ORB follow **or** fade — not both blindly)  
3. **VWAP** as context (side + bands), not sole trigger  
4. **RVOL / dry-up** matching the play  
5. **Index alignment** for breakouts; **index OR intact** for fades  
6. **ATR** for OR sizing + stop distance  
7. **Time window** (morning ORB; midday fade/chop rules)  
8. Optional: rejection candle, retest, news calendar skip  

### Avoid

- Trading every OR break  
- Fading 2σ on strong trend / ADX-high days  
- Midday **breakout** chasing on thin volume  
- Daily RRG / EOD chips as **intraday** primary signal  
- Treating VWAP cross as buy/sell  
- Optimizing win rate while ignoring expectancy  
- Claiming 80%+ without regime filters  

---

## 3. Map to our tracks

| Pro practice | Our status | Track / artifact |
|--------------|------------|------------------|
| Midday extreme fade | **Tested · core** | `fade_near_ext` ~62% OOS (`TA_30M_BIAS_EVAL`) |
| Fade only when **index OR intact** | **Tested · partial keep** | `fade_idx_or_inside` ~70.7% @0050 / ~66.9% @IX0001 (`TRACK_VS_MARKET`) |
| Standalone mom / VWAP / OR break as predictors | **Tested · fail** (~41–50%) | `TA_30M_BIAS_EVAL` |
| EOD 三大法人 / foreign streak as fade filter | **Tested · kill** | `TRACK_INSTITUTIONAL` |
| Vol surge chase / OR+surge | **Tested · fail** (~coin or worse) | `TRACK_PRICE_VOLUME` |
| Fade × vol dry-up | **Tested · observe only** (~68% explor.; not IS champ) | `TRACK_PRICE_VOLUME` |
| Daily RRG / WMA as 30m filter | **Tested · kill** | `TRACK_RRG_WMA` |
| Overnight RS vs IX0001 on fade | **Tested · weak** | `TRACK_VS_MARKET` |
| **Separate ORB breakout path** (RVOL+VWAP+RS) | **Not fully isolated** as research champion track | Bias grid had `or_break` but mixed with mom; no day-type fork |
| **Failed OR break → fade** (poke then close inside) | **Partial only** (OR-inside filter ≠ explicit fail-break event) | Gap |
| **VWAP ±2σ + rejection + ADX skip** | **Not pre-registered** | Gap |
| **RVOL time-of-day** (vs clock-bar history) | **Partial** (bar surge vs morning; not TOD RVOL) | Gap |
| **OR width / ATR exhaustion gate** | **Not tested** on fade30 | Gap |
| **Index trend filter** (idx above own VWAP / OR break) separate from OR-inside | **OR-inside only** | Gap |
| **Night H-L / gap** (TW futures style) | **Not tested** on stock 5m fade | Gap |
| CVD / footprint | **Out of scope** (data) | Blocked unless tick feed |
| Cross-track fusion AND | **Not run** | `TRACK_FUSION` absent |

**Alignment note:** Our best partial (`fade ∧ 0050 OR intact`) is exactly the pro intuition: **fade on rotational / OR-not-broken tape**. Failures of INST / daily RRG / raw PV chase match pro “avoid” list.

---

## 4. Next research experiments (aligned · not yet fully tested)

Pre-register under same metric lock as `RESEARCH_PLAN_MULTI_SIGNAL_70.md`. Research only.

### E1 · Fork: ORB path vs Fade path (day-type switch)

- **Fade path (frozen base):** keep `fade_idx_or_inside`.  
- **ORB path (new):** only when **0050 OR broken** with close beyond + stock same-side VWAP + RVOL≥1.5 on break bar; measure 30m directed hit **separately** (do not blend into fade champion).  
- **Hypothesis:** mixing ORB and fade in one thermometer collapses both edges; split should clarify.

### E2 · Explicit failed-break fade

- Event: stock (or index) **wicks beyond OR** then **5m close back inside** + poke RVOL ≤ 1.0×.  
- Compare to current `fade_near_ext` + OR-inside filter.  
- **Hypothesis:** event-defined fail-break > continuous “near extreme” label.

### E3 · VWAP 2σ + rejection + trend skip

- Midday: `|price−VWAP| ≥ 2σ` + rejection wick + **skip if** session ADX(14)@5m > 25 **or** OR width > 0.75× ATR(14).  
- AND with `fade_idx_or_inside` (IS-lock once).  
- **Hypothesis:** regime filter is what literature says separates 55–65% from ~45%.

### E4 · TOD RVOL dry-up ∧ idx_or_inside (IS-locked)

- Replace exploratory `fade_dryup_05` with **time-of-day RVOL** (vol / median same clock bar, prior N days).  
- Pre-register thresholds on IS only; single OOS read with `fade_idx_or_inside` frozen.  
- Matches Synthesis “next experiment”; closes OOS-peek risk on dry-up.

### E5 · Index **trend** filter (not only OR-inside) + ATR stop context

- Extra AND for fade: index **below own session VWAP slope flat/down** (for short fade of upside extremes) — or skip fades when index has already broken OR **and** holds outside for ≥K bars.  
- Report **MAE / ATR-normalized** adverse excursion (research risk context; not Order stops).  
- **Hypothesis:** explains IX0001 sensitivity gap (~70.7 → ~66.9).

**Hard-stop:** if E3–E5 fail to beat frozen champion on OOS gates, stop stacking volume/ADX ANDs (per Synthesis).

---

## 5. Citations (URLs fetched / searched 2026-07-24)

| # | Title | URL |
|---|-------|-----|
| 1 | Opening Range Breakout Strategy · Steady Turtle | https://steady-turtle.com/knowledge/opening-range-breakout-strategy |
| 2 | NQ ORB Strategy 2026 · Comborb | https://comborb.com/nq-orb-strategy |
| 3 | Context Engine day type · Comborb | https://comborb.com/context-engine-indicator |
| 4 | VWAP Trading Guide 2026 · Chart Whisperer | https://chartwhisperer.ca/blog/vwap-trading-guide |
| 5 | VWAP Reversion · CrossTrade | https://crosstrade.io/learn/trading-strategies/vwap-reversion |
| 6 | ORB Complete Guide · ChartingLens | https://chartinglens.com/blog/opening-range-breakout-strategy |
| 7 | ORB win rate honesty · The ORB Strategy Blog | https://theorbstrategy.com/blog/orb-strategy-win-rate-and-backtesting-basics/ |
| 8 | Midday chop playbook · DayTradingToolkit | https://daytradingtoolkit.com/strategies/traders-playbook-midday-chop |
| 9 | Opening range breakout refined · DXP | https://www.dxpa.in/resources/opening-range-breakout-refined |
| 10 | Breakout on Nifty · Intraday Lab | https://intradaylab.com/blog/breakout-trading-nifty-what-actually-works |
| 11 | ORB guide · GrandAlgo | https://grandalgo.com/blog/opening-range-breakout-strategy |
| 12 | Delta / CVD · United Daytraders | https://united-daytraders.com/blog/delta-cvd-advanced-order-flow |
| 13 | Order flow explained · D&T Systems | https://dtsystems.dev/blog/order-flow-trading-explained |
| 14 | Footprint charts · NinjaTrader | https://ninjatrader.com/futures/blogs/footprint-charts-guide/ |
| 15 | VWAP 當沖 · Leo / richkpi | https://richkpi.com/vwap-app/ |
| 16 | 台指期 VWAP + 夜盤高低 · 老墨 | https://mofiinvestment.com/blog/taiex-futures-vwap-night-session/ |
| 17 | VWAP 介紹 · 自營家 Peter | https://individual-trader.blogspot.com/2025/04/20250429-vwap.html |
| 18 | NexusFi ORB Academy (Crabel context) | https://nexusfi.com/a/strategies/opening-range-breakout |

Internal baselines: `SYNTHESIS_MULTI_SIGNAL_70.md`, `TRACK_VS_MARKET.md`, `TRACK_PRICE_VOLUME.md`, `TRACK_INSTITUTIONAL.md`, `TRACK_RRG_WMA.md`, `docs/1m-intraday-strategy-catalog.md`.

---

## 6. Bottom line

Pros run a **regime switch**: ORB follow on trend days, **fade failed extremes / VWAP stretch** on rotation — confirmed by volume, VWAP, index, and ATR — and treat EOD chips / daily RRG as **wrong tools** for 30m. Our research already rediscovered the fade×index-OR-intact core; the largest **untested** lifts are **path fork (ORB vs fade)**, **explicit fail-break**, **2σ+ADX skip**, **TOD RVOL**, and **index trend (not only OR-inside)**. Keep Live observe-only until a fresh holdout clears gates.
