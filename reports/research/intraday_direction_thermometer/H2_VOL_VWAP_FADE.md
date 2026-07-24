# H2 · Closed-30m RVOL / VWAP distance ∧ fade_idx_or_inside

Research only · **未採納** · not Order / not `strategy.yaml`

## Hypothesis

Adding **closed-30m relative volume** and/or **VWAP distance** filters to champion `fade_idx_or_inside` improves OOS directed hit **and** keeps n≥500 + multi-stock stability (majority of names OOS≥55%), vs champion alone (~70.66% n=634 from TRACK_BETA_EMA).

## Filters (PIT)

- **Closed-30m RVOL**: `sum(vol[-6:]) / median(prior same-session 6-bar volume sums)` (ends strictly before current window; min hist=3).
- **VWAP extreme**: `|close/VWAP−1|×100 ≥ thresh` and side agrees with fade (near-high → above VWAP; near-low → below).
- Base gate: midday `fade_near_ext` ∧ 0050 OR intact (`fade_idx_or_inside`).
- Distinct from E4 TOD RVOL and E3 VWAP±2σ rejection stacks.

## Metric / freeze

- **Directed hit (~30m)**: `temp∈{±1}` and `|fwd|≥0.0005`; hit iff `sign(temp)==sign(fwd)`.
- Forward: `close[i+6]/close[i]-1` @ 5m.
- PIT: fade / RVOL / VWAP / idx OR from completed same-session bars `≤ i`.
- IS / OOS: IS `≤ 2025-09-30`; champion on **IS only**; claim on OOS.
- Universe (ex `0050`): `2327, 8046, 3189, 6451, 2330, 2303, 2454`.
- Pre-registered variants: 14.
- Window: **2024-01-02 → 2026-07-22**
- H2 gate: OOS≥**60%** · n≥500 · majority (≥50%) names @≥55% (n≥50); also report strict ≥70% name stab (prior tracks).

## Verdict: H2 filters beat champion + stable？ **REJECT**

- Champion baseline `fade_idx_or_inside` OOS: **70.66%** (n=634)
- IS champion (H2 grid): `idx_rvol30_lo_0p5`
- Champion IS hit: **74.12%** (n=255)
- Champion OOS hit: **62.67%** (n=150)
- Δ OOS vs `fade_idx_or_inside`: **-7.99** pp
- Best OOS exploratory: `idx_rvol30_hi_2p0` → **93.41%** (n=91)
- Best OOS with n≥500: `fade_idx_or_inside` → **70.66%** (n=634)
- RVOL helped (best thick RVOL vs idx)? **UNKNOWN**
- VWAP-ext helped (best thick VWAP vs idx)? **NO (-0.12pp vs idx 70.66%)**
- Live adopt? **NO — keep Live observe on bare `fade_idx_or_inside`; do not volume-/VWAP-gate fade30 from this pass.**

## Gates（IS champion OOS）

- H2 majority name stab: `False` (name_pass=0/7=0.0; req≥0.5)
- Strict 70% name stab (prior): `False` (name_pass=0/7=0.0)
- `gate_oos_hit_ge60_n500`: **False**
- `gate_no_megacap_dom`: **True** (max share 0.267 · 2330)

## Pool leaderboard（OOS directed 排序）

|variant|family|IS hit%|IS n|OOS hit%|OOS n|Δ vs idx|
|---|---|---:|---:|---:|---:|---:|
|idx_rvol30_hi_2p0|h2|74.79|119|93.41|91|22.75|
|idx_rvol30_hi_1p5|h2|72.27|220|87.5|136|16.84|
|idx_rvol_hi_1p5_vwap_0p5|h2|72.47|178|82.8|93|12.14|
|idx_rvol30_hi_1p2|h2|73.8|332|82.39|176|11.73|
|fade_idx_or_inside|baseline|71.57|1333|70.66|634|0.0|
|idx_vwap_ext_0p3|h2|71.0|1269|70.54|611|-0.12|
|idx_vwap_ext_0p5|h2|68.18|971|65.48|478|-5.18|
|idx_rvol30_lo_0p7|h2|72.03|497|65.44|272|-5.22|
|idx_rvol30_lo_1p0|h2|71.27|804|64.99|397|-5.67|
|baseline_fade_near_ext|baseline|68.56|3158|64.02|1651|-6.64|
|idx_rvol30_lo_0p5|h2|74.12|255|62.67|150|-7.99|
|idx_rvol_lo_0p7_vwap_0p5|h2|68.38|351|62.25|204|-8.41|
|idx_vwap_ext_1p0|h2|62.63|380|60.94|297|-9.72|
|idx_vwap_ext_0p75|h2|66.18|618|60.87|368|-9.79|

## IS champion per-stock OOS · `idx_rvol30_lo_0p5`

|sid|OOS hit%|OOS n|E[signed]%|
|---|---:|---:|---:|
|2330|82.5|40|0.2781|
|8046|63.64|11|0.5515|
|2454|61.9|21|0.0711|
|2303|60.0|15|0.1044|
|6451|55.17|29|0.293|
|3189|52.63|19|-0.7274|
|2327|40.0|15|-0.2552|

## Champion baseline per-stock OOS · `fade_idx_or_inside`

|sid|OOS hit%|OOS n|E[signed]%|
|---|---:|---:|---:|
|2330|88.96|154|0.3255|
|2454|75.57|131|0.2544|
|2327|73.24|71|0.2084|
|8046|72.41|29|0.5192|
|2303|56.45|62|0.084|
|3189|55.71|70|-0.265|
|6451|55.56|117|0.0464|

## Leave-one-stock-out OOS · `idx_rvol30_lo_0p5`

|drop|OOS hit%|OOS n|
|---|---:|---:|
|2330|55.45|110|
|8046|62.59|139|
|2454|62.79|129|
|2303|62.96|135|
|3189|64.12|131|
|6451|64.46|121|
|2327|65.19|135|

## Interpretation

Champion `fade_idx_or_inside` OOS=70.66% (n=634; H2 majority gates=True). IS-locked H2 pick `idx_rvol30_lo_0p5` OOS=62.67% (n=150; Δ=-7.99pp; H2_gates=False; strict70=False; LOO min=55.45%). Best thick RVOL: none (all thin); best thick VWAP: idx_vwap_ext_0p3 OOS=70.54%. RVOL helped? UNKNOWN. VWAP helped? NO (-0.12pp vs idx 70.66%). Same class risk as E3/E4: filters that lift point estimate often thin n below 500 or fail name stability. Live: NO — keep Live observe on bare `fade_idx_or_inside`; do not volume-/VWAP-gate fade30 from this pass.

## Claim

- `h2_support`: **reject**
- `beats_champion_oos_thick`: **False**
- `oos_stable_h2_majority`: **False**
- `oos_stable_strict_70`: **False**
- `live_bias_updated`: **False**
- `order_adopted`: **False**

## Artifacts

- JSON: `reports/research/intraday_direction_thermometer/h2_vol_vwap_fade_30m.json`
- MD: `reports/research/intraday_direction_thermometer/H2_VOL_VWAP_FADE.md`
- Runner: `scripts/research/run_ta_30m_h2_vol_vwap_fade.py`

Generated: `2026-07-24T12:52:39`

