# Multi-signal 30m thermometer · Research plan（FROZEN）

Research only · **未採納** · not Order / not `strategy.yaml` · 2026-07-24

Parent topic: `intraday-direction-thermometer`（`config/research.yaml`）  
Anchor baseline: `baseline_fade_near_ext` · OOS directed **62.13%** (n=1846) — see `TA_30M_BIAS_EVAL.md`  
Sibling reject: Primary Swing 1h ~60m OOS best ≈51–53% — see `SWING_1H_EVAL.md`

## 1. Goal

Lift **~30m directed-hit** from the freeze fade baseline toward **OOS ≥ 70%** **and** pass the same **stability gates** as `TA_30M_BIAS_EVAL` — without kitchen-sink ML, without Live 70% claims, and without Order graduation.

| Gate (copied from TA_30M_BIAS_EVAL) | Threshold |
|-------------------------------------|-----------|
| `gate_oos_hit_ge70_n500` | OOS directed hit ≥ **70%** · pool `n ≥ 500` |
| `gate_name_stability` | ≥ **70%** of names with OOS hit ≥ **55%** and name `n ≥ 50` |
| `gate_no_megacap_dom` | Single name ≤ **40%** of pool n |
| Protocol | IS-only tuning；**claims only on OOS** |

### Metric lock（identical to TA_30M_BIAS_EVAL）

- **Primary directed hit (~30m)**: `temp ∈ {±1}` and `|fwd| ≥ 0.0005` (0.05%); hit iff `sign(temp) == sign(fwd)`.
- **Forward**: `close[i+6] / close[i] - 1`（≈30 分；同日 5m）。
- **PIT**: features only from completed same-day 5m bars `≤ i`（plus prior days’ daily facts where track allows, still `date ≤ T`）。
- **IS / OOS**: IS `trade_date ≤ 2025-09-30`；OOS `>`。 Champion / params from IS only.
- **Nulls to beat**: coin-flip 50%；always-long；`baseline_fade_near_ext`（**must improve OOS vs 62.13%**, not only vs coin）。
- **Window / universe** (default, unless a track pre-registers a subset):  
  `2024-01-02 → 2026-07-22` · `2327, 8046, 3189, 6451, 2330, 2303, 2454, 0050`.

**Success = all three gates True on the frozen IS-champion evaluated on OOS.**  
Partial wins (e.g. OOS 65–69% with better name stability) are logged as **observe candidates**, not 70% claims.

## 2. Why this round exists

| Prior result | Verdict |
|--------------|---------|
| TA 30m multi-factor grid (22 variants) | Best OOS = fade **62.13%**; mom/VWAP/OR/score/AND mostly **41–50%** |
| Name stability on fade | **5/8 = 62.5%** ＜ 70% gate（2330/2327/2454 strong; 3189/2303/0050 weak） |
| Swing 1h Primary ~60m | Rejected（best ~51–53%） |
| Live `ta_30m_bias` | Descriptive observe only — **not** a prediction claim |

Hypothesis for this program: **orthogonal information** (institutional flow, price–volume structure, vs-market residual, RRG/WMA posture) as **filters or soft votes on top of `fade_near_ext`**, not as replacements for the fade core.

## 3. Tracks（parallel agents）

Each track writes `reports/research/intraday_direction_thermometer/TRACK_<NAME>.md` with: metric lock restated, IS/OOS tables, gate booleans, kill/keep, and raw JSON path if any.

| Track ID | File | Hypothesis | Allowed v1 primitives |
|----------|------|------------|------------------------|
| **INST** | `TRACK_INSTITUTIONAL.md` | Same-day / lag-1 三大法人／外資淨額方向 or magnitude gates improve fade OOS or name stability | Net buy/sell flags, percentile / z of net, foreign vs total; PIT daily chip known by bar time (use prior close if same-day chip not yet available) |
| **PV** | `TRACK_PRICE_VOLUME.md` | Intraday volume surge / dry-up / VWAP distance / range position orthogonal to fade | Vol z vs morning, relative volume, VWAP side, OR break confirmation **as filter only** |
| **MKT** | `TRACK_VS_MARKET.md` | Stock vs IX0001 (or 0050) residual / relative 5m momentum reduces false fades on beta days | Stock−index signed move, beta-day skip, breadth proxy if PIT |
| **RRG** | `TRACK_RRG_WMA.md` | Daily W20 RRG / WMA posture (ratio/momentum) filters names where fade fails | `rs_ratio` / Momentum gates, Stage-ish WMA slope; daily PIT only |
| **FUSION** | `TRACK_FUSION.md`（reserved synthesizer + optional agent） | Combine **kept** track filters under §4 rules | AND / score vote / TOD only — see below |

Tracks that cannot run (missing data) must still file a short TRACK note with `status: blocked` and reason — do not invent numbers.

## 4. Fusion rules allowed（v1）

**Allowed**

1. **AND filters on `fade_near_ext`**: keep fade temp only when track filter passes (e.g. fade ∧ foreign_not_heavy ∧ not_beta_day).
2. **Score voting**: discrete votes `{−1,0,+1}` from ≤4 kept primitives; fire when `|score| ≥ K` and sign agrees with fade (or score alone if pre-registered).
3. **Time-of-day (TOD)**: midday window already in fade; optional open_guard / last-N-bars mute as AND.
4. **Stock subset observe**: report per-name OOS; may propose Tier-A name list for research — **not** a pool 70% claim unless pool gates pass.

**Not allowed in v1**

- Kitchen-sink ML / GBM / neural nets / large free grids over OOS.
- Post-hoc expansion of the variant grid to cherry-pick OOS champions.
- Claiming Live `ta_30m_bias` or any thermometer is **OOS ≥ 70%** without gates.
- Writing `config/strategy.yaml` / Order live / launchd.
- Using Secondary 30m success to claim Swing 1h / 60m gates.

## 5. Kill criteria（per track）

Kill a track (no merge into fusion) if **any**:

1. **No OOS lift**: IS-champion’s OOS hit ≤ `baseline_fade_near_ext` OOS (**62.13%**) **and** no material stability improvement (name-stability rate not ↑ by ≥1 name clearing 55%@n≥50, or megacap share not improved while hit flat).
2. **Stability hurt**: OOS hit rises but `gate_name_stability` worsens vs fade baseline, or one name share ＞40%.
3. **Sample collapse**: OOS `n < 500` after filter (cannot claim pool gate); may keep as **sparse observe** only if pre-registered as such — still kill for fusion v1.
4. **IS/OOS flip**: large IS edge that disappears or reverses on OOS (decay to ≤ coin + 2pp without stability win).
5. **Non-PIT / lookahead** found in review → hard kill + reject note.

## 6. Synthesizer deliverables

| Artifact | When |
|----------|------|
| This plan `RESEARCH_PLAN_MULTI_SIGNAL_70.md` | Immediate（frozen） |
| Stub / final `SYNTHESIS_MULTI_SIGNAL_70.md` | When ≥1 TRACK_*.md lands；finalize when INST/PV/MKT/RRG present or timeout |
| Optional `config/research.yaml` notes | Only if user asks； hypotheses stay research |

Synthesis must state:

- Best **single** filter (track + rule)
- Best **AND** combo among kept filters
- Whether **70% + stability** hit
- Recommendation: keep research / observe overlay / hard-stop

## 7. What is NOT allowed（program-wide）

- Claiming **Live 70%** without OOS gates True.
- **Order graduation** / `strategy.yaml` adoption from this round.
- Relabeling Swing 1h Primary as passed via 30m secondary.
- Hiding kill results; every track files keep/kill explicitly.

## 8. Output paths

```
reports/research/intraday_direction_thermometer/
  RESEARCH_PLAN_MULTI_SIGNAL_70.md   ← this file
  TRACK_INSTITUTIONAL.md
  TRACK_PRICE_VOLUME.md
  TRACK_VS_MARKET.md
  TRACK_RRG_WMA.md
  TRACK_FUSION.md                    ← optional
  SYNTHESIS_MULTI_SIGNAL_70.md
```

Runners (expected / existing): `scripts/research/run_ta_30m_bias_backtest.py` and track-specific scripts under `scripts/research/` · module `src/research/intraday_direction_thermometer.py` (`fade_near_ext_from_bars`).

## 9. Frozen decision log

| Date | Event |
|------|-------|
| 2026-07-24 | Plan frozen from TA_30M_BIAS_EVAL + SWING_1H_EVAL gates; multi-track hunt for OOS≥70% started |
| *(pending)* | TRACK_* land → synthesis |
| *(pending)* | Final keep/kill + 70% verdict |

---

Generated plan: 2026-07-24 · synthesizer agent
