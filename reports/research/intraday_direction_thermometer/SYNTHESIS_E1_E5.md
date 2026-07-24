# E1–E5 pro-methods · Synthesis

Research only · **未採納** · not Order / not `strategy.yaml` · 2026-07-24

Plan SSOT: [`RESEARCH_PLAN_E1_E5.md`](./RESEARCH_PLAN_E1_E5.md)  
Design source: [`PRO_METHODS_WEB_REVIEW.md`](./PRO_METHODS_WEB_REVIEW.md) §4  
Prior champion: `fade_idx_or_inside` OOS **70.66%** (n=634 · 0050) — [`SYNTHESIS_MULTI_SIGNAL_70.md`](./SYNTHESIS_MULTI_SIGNAL_70.md)

## Verdict (one line)

**No new E1–E5 rule clears OOS≥70% + stability gates.** Frozen research champion remains **`fade_idx_or_inside` only**. ORB path / fail-break / VWAP-2σ stack / TOD dry-up / index-trend ANDs all fail claim gates (hit, n, or stability). **Still no Order**; Live observe-only unchanged. Hard-stop: **stop stacking** volume/ADX ANDs.

## Track roll-up

| Track | File | IS champion / claim rule | Champ OOS | OOS n | Gates | Keep / Kill |
|-------|------|--------------------------|----------:|------:|-------|-------------|
| E1 | `TRACK_E1_ORB_FADE_SPLIT.md` | Fade: `fade_idx_or_inside`；ORB: `orb_break_held_rvol15_vwap` | Fade **70.66%** / ORB **46.31%** / Union **50.17%** | 634 / 2274 / 2657 | Fade **PASS**；ORB/Union **FAIL** | **KEEP fade path**（reproduce MKT）；**KILL ORB** for 70% claim；**do not union** |
| E2 | `TRACK_E2_FAIL_BREAK.md` | `fb_n3` | **38.95%** | 4734 | all False | **Kill** |
| E3 | `TRACK_E3_VWAP_BANDS.md` | `e3_full`（VWAP2σ∧rej∧skip∧idx） | **n=0** | 0 | all False | **Kill**（sample collapse） |
| E4 | `TRACK_E4_TOD_RVOL.md` | `idx_tod_dry_0p5_L10` | **71.76%** | **131** | hit True-ish · **n＜500** · stab False | **Kill claim**；sparse **observe only** |
| E5 | `TRACK_E5_INDEX_TREND_ATR.md` | `fade_or_inside_and_vwap_mr`（0050 IS） | **70.7%** | **273** | n＜500 · stab False | **Kill claim**；gap explained · no IX≥70@n500 |

### Gate detail · only prior pass still stands

| Rule | Track | `hit≥70∧n≥500` | name stab | megacap | Claim? |
|------|-------|:--------------:|:---------:|:-------:|--------|
| `fade_idx_or_inside` | E1 / prior MKT | **True** (70.66 · 634) | **True** (6/7) | **True** | **YES（research）** |
| `fade_stock_or_intact` | E1 | False (73.11 · **383**) | False | True | NO |
| ORB stack / unions | E1 | False (~46–50%) | — | — | NO |
| Fail-break family | E2 | False (~39%) | False | True | NO |
| `e3_full` | E3 | False (n=0) | — | — | NO |
| TOD dry-up ∧ idx | E4 | False (71.76 · **131**) | False | True | NO（peek-safe but thin） |
| Index trend ANDs | E5 | False (thin n) | False | True | NO |
| IX0001 + trend | E5 | False (best explor. 70.92 · **282**) | — | — | NO |

## Best single filter

| Rank | Rule | Source | Protocol | OOS hit | OOS n | Notes |
|------|------|--------|----------|--------:|------:|-------|
| **1 (claim)** | `fade_near_ext` ∧ **0050 OR intact** | E1 / MKT | IS→OOS gates | **70.66%** | 634 | Sole gate passer this program |
| 2 (E1 explor.) | fade ∧ stock OR intact | E1 | hit high · n fail | 73.11% | 383 | Cannot claim pool gate |
| 3 (E4 explor.) | idx ∧ TOD dry 0.7× L10 | E4 | not IS champ / thin | 74.02% | 204 | Do not rebrand as claim |
| 4 (E4 IS champ) | idx ∧ TOD dry 0.5× L10 | E4 | IS-locked · thin | 71.76% | 131 | Honest peek-fix；still kill claim |
| — | Unfiltered fade | prior | anchor | 62–64% | ~1650–1846 | Null |

## Best AND combo

| Candidate | Status | Rationale |
|-----------|--------|-----------|
| **fade ∧ idx_or_inside** | **Keep as research champion** | Only full gate passer |
| fade ∧ idx ∧ TOD dry-up | **Reject for claim** | E4: OOS hit can look ≥70% but **n≪500** + name stab fail |
| fade ∧ idx ∧ VWAP2σ/ADX | **Reject** | E3: `e3_full` **n=0**；standalone VWAP2σ ~42% |
| fade ∧ idx ∧ index VWAP/hold | **Reject for claim** | E5: thins n；IX0001 still ＜70@n500 |
| fade ∪ ORB (day-type union) | **Reject** | E1: union OOS ~47–50%（ORB dilutes fade） |
| Explicit fail-break | **Reject** | E2: ~39% ≪ fade |

Score-voting / kitchen-sink: **not used**.

## Was 70% achieved across E1–E5?

| Question | Answer |
|----------|--------|
| Any **new** track clears OOS≥70% + stability + n≥500? | **NO** |
| Does prior `fade_idx_or_inside` still pass (reproduced in E1)? | **YES** |
| Does ORB path clear 70%? | **NO** (~46% filtered) |
| Does fail-break beat fade / idx? | **NO** (~39%) |
| E3–E5 beat frozen champion on gates? | **NO** → **hard-stop stacking** |
| Live `ta_30m_bias` now “70%”? | **NO** |
| Order / `strategy.yaml`? | **Forbidden** · **still no Order** |

### Caveats (unchanged + new)

1. Champion bench is **0050 5m**, not TAIEX；IX0001 or_inside OOS ~**66.9%** (E5).
2. Gap source (E5): days where **0050 OR intact but IX0001 already broke** hit ~69.9% vs agree-inside ~73.6% — proxy labels rotation while index has trend-broken.
3. E4 hit% ≥70 on thin n is **not** a claim (same class of error as prior dry-up peek).
4. E3 confluence stack emptied the sample — literature filters are too tight on this 5m universe/definition.
5. Pros’ ORB win-rate band (~40–65%) matches our ORB path；**70% directed-hit is not an ORB target**.

## Kill / keep log

| Item | Decision | Reason |
|------|----------|--------|
| `fade_idx_or_inside` | **Keep**（research champion） | Sole gate passer；E1 reproduced |
| ORB follow path | **Kill** for thermometer 70% | OOS ~46–50%；union dilutes fade |
| Day-type **fork** (separate metrics) | **Keep as method** | Confirms: do **not** blend ORB into fade thermometer |
| Fail-break event fade | **Kill** | ≪ fade；wrong primary for 30m directed-hit |
| VWAP 2σ + ADX/wide-OR AND | **Kill** | n=0 or ≪ fade |
| TOD RVOL ∧ idx | **Kill claim** / observe sparse | n＜500；stab fail |
| Index trend AND beyond OR-inside | **Kill claim** | No IX≥70@n500；does not beat champion |
| Further volume/ADX AND stacks | **Hard-stop** | Plan §2 / PRO §4 triggered by E3–E5 |

## Live / Order recommendation

| Surface | Recommendation |
|---------|----------------|
| **Order layer** | **No** — do not write `strategy.yaml` / do not live-submit from this round |
| **Live `ta_30m_bias`** | **Observe only** — do **not** advertise OOS≥70%；optional future overlay language only after **fresh holdout** |
| **Research thermometer** | Document `fade_idx_or_inside` as frozen research champion；ORB/fail-break/VWAP stacks stay killed |

## Next experiments（narrow）

1. **Fresh holdout** for frozen `fade_idx_or_inside`（post-2026-07-22 or leave-one-year）before any Live overlay wording.  
2. **IX0001 data / proxy honesty**: treat 0050-pass as **proxy claim** only；do not equate to TAIEX until IX bars cover IS.  
3. Optional: ORB as a **separate** research path with **expectancy/R:R** metrics（not 70% directed-hit gate）— out of scope for this thermometer program.  
4. **Do not**: more TOD/ADX/VWAP AND grids on fade；do not promote E4 thin-n ≥70%；do not union ORB+fade.

## Artifacts

| Path | Role |
|------|------|
| `RESEARCH_PLAN_E1_E5.md` | Frozen plan |
| `TRACK_E1_ORB_FADE_SPLIT.md` + `track_e1_orb_fade_split.json` | Fade keep · ORB kill |
| `TRACK_E2_FAIL_BREAK.md` + `track_e2_fail_break.json` | Kill |
| `TRACK_E3_VWAP_BANDS.md` + `track_e3_vwap_bands.json` | Kill |
| `TRACK_E4_TOD_RVOL.md` + `track_e4_tod_rvol.json` | Kill claim |
| `TRACK_E5_INDEX_TREND_ATR.md` + `track_e5_index_trend_atr.json` | Kill claim · gap note |
| `PRO_METHODS_WEB_REVIEW.md` | Design source |
| `SYNTHESIS_MULTI_SIGNAL_70.md` | Prior champion |

---

Synthesis finalized: 2026-07-24 · synthesizer
