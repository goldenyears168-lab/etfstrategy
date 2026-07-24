# Track E1 · ORB path vs Fade path 分軌 · ~30m

Research only · **未採納** · not Order / not `strategy.yaml`

Parent: `intraday-direction-thermometer` · Plan: [`RESEARCH_PLAN_MULTI_SIGNAL_70.md`](./RESEARCH_PLAN_MULTI_SIGNAL_70.md) · Pro: [`PRO_METHODS_WEB_REVIEW.md`](./PRO_METHODS_WEB_REVIEW.md) §E1

## Regime definition（互斥 · after 09:30）

- **OR** = first `6` × 5m bars（≈09:00–09:30）high/low。
- **`orb_broken_held`**: 最近 `hold_bars` 根收盤皆在 OR 同側之外（close 突破且持住）。
- **`or_intact`**: 收盤仍在 OR 內 → 允許 fade。
- **`or_broken_unheld`**: 當根在外但未達 held 連續條件（不發 ORB；fade 也不發）。
- Paths **不混用**：intact 只跑 fade；broken&held 只跑 ORB follow。

## Metric / freeze

- **Directed hit (~30m)**: `temp∈{±1}` and `|fwd|≥0.0005`; hit iff `sign(temp)==sign(fwd)`.
- Forward: `close[i+6]/close[i]-1` @ 5m.
- PIT: completed same-session bars `≤ i` only.
- IS / OOS: IS `≤ 2025-09-30`; champion on **IS only**; claim on OOS.
- Universe: `2327, 8046, 3189, 6451, 2330, 2303, 2454, 0050` （bench variants ex-`0050`）。
- Pre-registered variants: 12.
- Window: **2024-01-02 → 2026-07-22**

## Verdict: either/both paths clear 70%+stability？ **PARTIAL — only fade∧idx-OR-intact；ORB path fail**

- Fade path clears? **YES** （`fade_idx_or_inside`）
- ORB path clears? **NO** （`orb_break_held_rvol15_vwap`）
- Union clears? **NO**
- IS champion: `fade_stock_or_intact`
- Champion IS: **75.29%** (n=955)
- Champion OOS: **73.11%** (n=383)
- Best OOS exploratory: `fade_stock_or_intact` → **73.11%** (n=383)
- Keep / Kill: **KEEP fade path (research reproduce MKT)；KILL ORB path for 70% claim；do not union**

## Path roll-up（OOS）

|path|variant|OOS hit%|OOS n|gates|note|
|---|---|---:|---:|---|---|
|fade_baseline|baseline_fade_near_ext|62.13|1846|FAIL|unfiltered midday fade|
|fade_intact|fade_stock_or_intact|73.11|383|FAIL|fade ∧ stock OR inside|
|fade_idx|fade_idx_or_inside|70.66|634|PASS|MKT champion · 0050 OR inside|
|orb_raw|orb_break_held|50.36|20888|FAIL|close beyond OR held|
|orb_stack|orb_break_held_rvol15_vwap|46.31|2274|FAIL|ORB + RVOL≥1.5 + VWAP side|
|union_stock|union_fade_intact_orb_stack|50.17|2657|FAIL|mutually exclusive union|
|union_idx|union_fade_idx_orb_stack|47.41|1928|FAIL|fade_idx ∪ orb_stack|

## Combined coverage

- Fade path OOS n: **383**
- ORB path OOS n: **2274**
- Union OOS n: **2657**
- Union vs fade-alone coverage ratio: **6.937**
- Overlap note: Union n ≈ fade_intact + orb_stack by construction (fade=383, orb=2274, union=2657); regimes mutually exclusive so no double-count.

## Gates（IS champion OOS）

- `gate_oos_hit_ge70_n500`: **False**
- `gate_name_stability`: **False** (3/8 = 0.375)
- `gate_no_megacap_dom`: **True** (max share 0.397 · 2330)

## Pool leaderboard（OOS directed 排序）

|variant|family|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|Δ vs fade|
|---|---|---:|---:|---:|---:|---:|---:|
|fade_stock_or_intact|fade_path|75.29|955|73.11|383|23.11|10.98|
|fade_idx_or_inside|fade_path|71.57|1333|70.66|634|20.66|8.53|
|baseline_fade_near_ext|baseline|66.86|3467|62.13|1846|12.13|0.0|
|baseline_orb_break_raw|baseline|50.35|27381|50.36|20888|0.36|-11.77|
|orb_break_held|orb_path|50.35|27381|50.36|20888|0.36|-11.77|
|union_fade_intact_orb_stack|union|55.92|3714|50.17|2657|0.17|-11.96|
|orb_break_held_vwap|orb_path|50.33|27000|49.62|19748|-0.38|-12.51|
|orb_break_held2_rvol15_vwap|orb_path|51.69|1981|48.95|1912|-1.05|-13.18|
|union_fade_idx_orb_stack|union|53.59|2691|47.41|1928|-2.59|-14.72|
|orb_break_held_rvol15|orb_path|49.23|2781|46.85|2352|-3.15|-15.28|
|orb_break_held_rvol15_vwap|orb_path|49.22|2759|46.31|2274|-3.69|-15.82|
|orb_break_idx_confirm_rvol15_vwap|orb_path|47.83|1131|44.47|922|-5.53|-17.66|

## IS champion per-stock OOS · `fade_stock_or_intact`

|sid|OOS hit%|OOS n|E[signed]%|
|---|---:|---:|---:|
|2330|82.89|152|0.268|
|2454|74.51|102|0.2203|
|8046|71.43|7|0.5003|
|6451|67.92|53|0.1184|
|2327|66.67|12|0.0629|
|3189|66.67|6|0.4335|
|2303|55.56|27|0.054|
|0050|41.67|24|-0.0869|

## Interpretation

Day-type fork after OR: fade on intact OR OOS=73.11% (n=383); ORB stack (RVOL≥1.5+VWAP) OOS=46.31% (n=2274). Index-OR fade (`fade_idx_or_inside`) OOS=70.66% (n=634) — reproduces MKT partial champion. Union coverage OOS hit=50.17% (n=2657). Pros expect ORB raw ~40–52% and filtered ORB ~52–65%; our ORB path is in that band and does **not** clear 70%. Fade∧OR-intact remains the only near-70% path. Do not blend ORB into the fade thermometer; keep paths separate. Research only — no Live 70% claim, no Order.

## Artifacts

- JSON: `reports/research/intraday_direction_thermometer/track_e1_orb_fade_split.json`
- MD: `reports/research/intraday_direction_thermometer/TRACK_E1_ORB_FADE_SPLIT.md`
- Runner: `scripts/research/run_track_e1_orb_fade_split.py`

Generated: `2026-07-24T11:47:32`

