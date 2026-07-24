# E1–E5 pro-methods experiments · Research plan（FROZEN）

Research only · **未採納** · not Order / not `strategy.yaml` · 2026-07-24

Parent: `intraday-direction-thermometer`（`config/research.yaml`）  
Prior SSOT: [`PRO_METHODS_WEB_REVIEW.md`](./PRO_METHODS_WEB_REVIEW.md) §4 · [`SYNTHESIS_MULTI_SIGNAL_70.md`](./SYNTHESIS_MULTI_SIGNAL_70.md)  
Frozen champion (research claim): `fade_idx_or_inside` · OOS **70.66%** (n=634 · 0050 proxy)  
Anchor: `baseline_fade_near_ext` OOS **62.13%** (n=1846)

## 1. Goal

Test the five **untested pro lifts** from the web review — day-type fork, fail-break event, VWAP 2σ+ADX skip, TOD RVOL dry-up, index trend+ATR — under the **same metric lock and gates** as `RESEARCH_PLAN_MULTI_SIGNAL_70.md`.

**Success** = IS-locked champion clears **all three gates** on OOS.  
Partial lifts (65–69% / stability-only) → **observe**, not 70% claims.  
Default Live/Order stance: **no Order**; Live remains observe-only until a fresh holdout.

## 2. Shared gates（copied · locked）

| Gate | Threshold |
|------|-----------|
| `gate_oos_hit_ge70_n500` | OOS directed hit ≥ **70%** · pool `n ≥ 500` |
| `gate_name_stability` | ≥ **70%** of names with OOS hit ≥ **55%** and name `n ≥ 50` |
| `gate_no_megacap_dom` | Single name ≤ **40%** of pool n |
| Protocol | IS-only tuning；**claims only on OOS** |

### Metric lock

- **Primary directed hit (~30m)**: `temp ∈ {±1}` and `|fwd| ≥ 0.0005` (0.05%); hit iff `sign(temp) == sign(fwd)`.
- **Forward**: `close[i+6] / close[i] - 1`（≈30 分；同日 5m）。
- **PIT**: features only from completed same-day 5m bars `≤ i`（plus prior days’ daily facts where allowed, still `date ≤ T`）。
- **IS / OOS**: IS `trade_date ≤ 2025-09-30`；OOS `>`。 Champion / params from IS only.
- **Nulls to beat**: coin 50%；`baseline_fade_near_ext`（62.13%）；and for fade filters, frozen `fade_idx_or_inside`（70.66% @0050）when the track is an **AND stack**.
- **Window / universe** (default): `2024-01-02 → 2026-07-22` · `2327, 8046, 3189, 6451, 2330, 2303, 2454, 0050`（MKT-style ex-0050 when 0050 is bench）。

**Hard-stop (from PRO §4):** if E3–E5 fail to beat frozen champion on OOS gates, **stop stacking** volume/ADX ANDs.

## 3. Tracks E1–E5

Each track writes `TRACK_E*_*.md` + optional runner under `scripts/research/`. Commit only that track’s files.

| ID | File | Goal | Hypothesis |
|----|------|------|------------|
| **E1** | `TRACK_E1_ORB_FADE_SPLIT.md` | Fork: ORB follow path vs fade path by day-type (OR broken+held vs OR intact) | Mixing ORB+fade collapses both edges; split clarifies |
| **E2** | `TRACK_E2_FAIL_BREAK.md` | Explicit fail-break fade (wick beyond OR → close inside + poke RVOL≤1.0) | Event-defined fail-break > continuous near-extreme |
| **E3** | `TRACK_E3_VWAP_BANDS.md` | Midday VWAP 2σ + rejection + ADX/wide-OR skip ∧ `fade_idx_or_inside` | Regime filter lifts literature 55–65% toward our 70% gate |
| **E4** | `TRACK_E4_TOD_RVOL.md` | TOD RVOL dry-up ∧ `fade_idx_or_inside`（IS-lock thresholds；fix dry-up peek） | Proper TOD RVOL closes OOS-peek risk on ~68% explor. |
| **E5** | `TRACK_E5_INDEX_TREND_ATR.md` | Index trend filter beyond OR-inside + MAE/ATR context；0050 vs IX0001 | Explains ~70.7→66.9 gap; may push IX0001 ≥70% |

### Per-track deliverables

1. Metric lock restated + IS/OOS tables  
2. Gate booleans (`all_gates_pass`)  
3. Keep / Kill / Observe  
4. Raw JSON path if any  
5. **Do not** claim Live 70% or touch Order / `strategy.yaml`

## 4. Kill criteria（per track）

Kill (no merge into next fusion) if **any**:

1. **No OOS lift** vs relevant null（fade 62.13% for new paths；`fade_idx_or_inside` for AND stacks）and no material stability win.  
2. **Stability hurt** or megacap share ＞40%.  
3. **Sample collapse** OOS `n < 500`（sparse observe only if pre-registered）.  
4. **IS/OOS flip**（IS edge → OOS ≤ coin+2pp without stability win）.  
5. **Non-PIT / lookahead** → hard kill.

## 5. Synthesizer deliverables

| Artifact | When |
|----------|------|
| This plan `RESEARCH_PLAN_E1_E5.md` | Immediate（frozen） |
| `SYNTHESIS_E1_E5.md` | When E1–E5 TRACK_*.md land（or ~30–40 min timeout） |

Synthesis must state: which tracks pass OOS≥70%+stability；best single / best AND combo；Live/Order recommendation（default **still no Order**）；next kill/keep.

## 6. Output paths

```
reports/research/intraday_direction_thermometer/
  PRO_METHODS_WEB_REVIEW.md          ← design source
  RESEARCH_PLAN_E1_E5.md             ← this file
  TRACK_E1_ORB_FADE_SPLIT.md
  TRACK_E2_FAIL_BREAK.md
  TRACK_E3_VWAP_BANDS.md
  TRACK_E4_TOD_RVOL.md
  TRACK_E5_INDEX_TREND_ATR.md
  SYNTHESIS_E1_E5.md
```

## 7. Frozen decision log

| Date | Event |
|------|-------|
| 2026-07-24 | Plan frozen from PRO_METHODS_WEB_REVIEW §4 + MULTI_SIGNAL_70 gates；E1–E5 agents launched |
| *(pending)* | TRACK_E* land → synthesis |
| *(pending)* | Final keep/kill + 70% verdict |

---

Generated plan: 2026-07-24 · synthesizer agent
