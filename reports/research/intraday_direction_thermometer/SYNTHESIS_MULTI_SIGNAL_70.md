# Multi-signal 30m · Synthesis

Research only · **未採納** · not Order / not `strategy.yaml` · 2026-07-24

Plan SSOT: [`RESEARCH_PLAN_MULTI_SIGNAL_70.md`](./RESEARCH_PLAN_MULTI_SIGNAL_70.md)  
Anchor: `baseline_fade_near_ext` OOS **62.13%** (n=1846) · prior gates failed (`TA_30M_BIAS_EVAL.md`)

## Verdict (one line)

**OOS ≥ 70% + stability: YES — only via Track MKT `fade_idx_or_inside`** (OOS **70.66%**, n=634, 6/7 names ≥55%). Other tracks do **not** clear gates; do **not** Order-graduate or rewrite Live bias without a fresh holdout.

## Track roll-up

| Track | File | IS champion | Champ OOS | Best explor. OOS | Gates | Keep / Kill |
|-------|------|-------------|----------:|-----------------:|-------|-------------|
| INST | `TRACK_INSTITUTIONAL.md` | `fade_foreign_streak3` | **54.84%** (n=217) | `fade_trust_buy_t1` **66.63%** | all False | **Kill** (IS overfit; champ hurts fade) |
| PV | `TRACK_PRICE_VOLUME.md` | `fade_ud_agree` | **59.81%** (n=520) | `fade_dryup_05` **68.09%** | hit/stab False | **Kill claim**; dry-up = observe only |
| MKT | `TRACK_VS_MARKET.md` | `fade_idx_or_inside` | **70.66%** (n=634) | same | **all True** | **Keep** (research claim) |
| RRG | `TRACK_RRG_WMA.md` | `fade_price_vs_wma5_align` | **60.77%** (n=938) | `fade_price_vs_wma5_contra` **64.79%** | hit/stab False | **Kill** (champ ≤ fade; native RRG ~coin) |
| FUSION | — | — | — | — | — | **Not run** (no `TRACK_FUSION.md`) |

### Gate detail · kept champion (`fade_idx_or_inside`)

| Gate | Result |
|------|--------|
| `gate_oos_hit_ge70_n500` | **True** (70.66% · n=634) |
| `gate_name_stability` | **True** (6/7 = 0.857 · floor 55%@n≥50) |
| `gate_no_megacap_dom` | **True** (max share 0.243 · 2330) |

Universe for MKT is **ex-0050** (0050 used as 5m bench). Fade baseline on that 7-name set is ~64% OOS in the MKT table — still a clear lift to ~70.7%.

## Best single filter

| Rank | Rule | Track | Protocol role | OOS hit | OOS n | Notes |
|------|------|-------|---------------|--------:|------:|-------|
| **1 (claim)** | `fade_near_ext` ∧ **0050 OR still intact** (`fade_idx_or_inside`) | MKT | IS-locked champion | **70.66%** | 634 | Only rule clearing all three gates |
| 2 (observe) | `fade_near_ext` ∧ vol dry-up ≤0.5× (`fade_dryup_05`) | PV | Exploratory OOS peak | 68.09% | 796 | **Not** IS champion → cannot claim |
| 3 (observe) | `fade_near_ext` ∧ trust buy T−1 | INST | Exploratory | 66.63% | 830 | Champ was streak3 (killed) |
| 4 (observe) | `fade_near_ext` ∧ price vs WMA5 contra | RRG | Exploratory | 64.79% | 869 | Champ was align (killed) |
| — | Unfiltered `baseline_fade_near_ext` | prior | Anchor | 62.13% | 1846 | 8-name pool |

**Definition (kept):** fire midday fade only when the **0050 opening range has not broken** (index OR still inside). Standalone RS / vs-index VWAP / ORB without fade remain ~coin-flip.

## Best AND combo

No joint AND grid was executed across tracks (`TRACK_FUSION` absent).

| Candidate | Status | Rationale |
|-----------|--------|-----------|
| **fade ∧ idx_or_inside** | **Adopt as research champion** | Sole IS→OOS gate passer |
| fade ∧ idx_or_inside ∧ dryup_05 | **Proposed next IS-lock** | PV dry-up is the closest exploratory lift (+~6pp vs fade) but must be **re-selected on IS only** with MKT filter frozen — do not peek OOS |
| fade ∧ idx_or_inside ∧ trust_buy_t1 | Low priority | INST T−1 chip lag; exploratory only; streak filters toxic |
| fade ∧ RRG/WMA align | **Reject** | Champ OOS below fade; daily RRG is wrong horizon for 30m |

Score-voting / kitchen-sink ML: **not used** (plan v1 ban). TOD already embedded in fade midday window.

## Was 70% achieved across tracks?

| Question | Answer |
|----------|--------|
| Any track clears OOS≥70% + stability + n≥500? | **YES — MKT only** |
| INST / PV / RRG clear? | **NO** |
| Cross-track fusion clears? | **Not tested** |
| Live `ta_30m_bias` now “70%”? | **NO** — research claim only; needs fresh holdout before Live wording |
| Order / `strategy.yaml`? | **Forbidden** this round |

### Caveats on the YES

1. Bench is **0050 5m**, not TAIEX; `IX0001` 5m has **0 IS days** under the freeze cut.
2. Pool **n=634** barely clears 500; thinner than unfiltered fade.
3. Soft names (2303/3189/6451) sit near the **55%** floor.
4. Prior leave-one-stock fragility on raw fade (2330-heavy) still relevant — 2330 OOS **88.96%** on champion.
5. Exploratory peaks on other tracks (dry-up 68%) must **not** be rebranded as champions.

## Kill log (plan §5)

| Track | Kill reason |
|-------|-------------|
| INST | Champ OOS ≪ fade (−7.3pp); n＜500; name stab 0/8; megacap share fail |
| PV | Champ OOS 59.8% ＜ fade; stab 3/8; dry-up observe-only (OOS-peek ban) |
| RRG | Champ OOS ≤ fade; stab 4/8; naive RRG→30m ~49% |
| MKT | — kept |

## Recommendation

1. **Research champion (frozen):** `fade_idx_or_inside` — document in thermometer notes as **observe overlay candidate**, not Live prediction copy.
2. **Next experiment (optional):** one pre-registered IS grid of `{dryup thresholds} × idx_or_inside` → single OOS read; if fails, **hard-stop** further volume ANDs.
3. **Do not:** rewrite Live `ta_30m_bias` as OOS≥70%; do not touch Order / `strategy.yaml`; do not promote INST streak or RRG-as-30m predictor.
4. **Fresh holdout:** before any Live overlay language, lock params and evaluate a post-2026-07-22 (or leave-one-year) window not used in this OOS.

## Artifacts

| Path | Role |
|------|------|
| `TRACK_INSTITUTIONAL.md` | Kill |
| `TRACK_PRICE_VOLUME.md` | Kill claim / dry-up observe |
| `TRACK_VS_MARKET.md` | **Keep · gate pass** |
| `TRACK_RRG_WMA.md` | Kill |
| `vs_market_30m_backtest.json` | MKT numbers |
| `TA_30M_BIAS_EVAL.md` / `SWING_1H_EVAL.md` | Prior rejects |

---

Synthesis finalized: 2026-07-24 · synthesizer
