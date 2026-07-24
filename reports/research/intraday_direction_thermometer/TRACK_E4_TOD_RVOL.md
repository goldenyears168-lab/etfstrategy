# TRACK E4 · TOD RVOL dry-up ∧ idx_or_inside · ~30m

Research only · **未採納** · not Order / not `strategy.yaml`

## Goal

- Replace exploratory session-mean `fade_dryup_05` (OOS~68%, **peek risk**) with **time-of-day RVOL** dry-up.
- AND with `fade_near_ext` and/or `fade_idx_or_inside`.
- **Freeze thresholds on IS only**; single OOS claim.

## Metric / freeze

- **Directed hit (~30m)**: `temp ∈ {±1}` 且 `|fwd| ≥ 0.0005`；hit iff `sign(temp)==sign(fwd)`。
- **Forward**: `close[i+6]/close[i]-1`（≈30 分 @ 5m）。
- **TOD RVOL**: `vol / median(same HH:MM over prior L days)`；min hist=5；PIT（不含當日／未來日）。
- **PIT**: fade / index OR 僅用當日已完成 bars `≤ i`。
- **IS / OOS**: IS ≤ `2025-09-30`；OOS `>`。選冠軍只看 IS；**主閘只認一次 OOS**。
- Bench: `0050` 5m OR-inside（同 TRACK_VS_MARKET）。
- Universe (ex bench): `2327, 8046, 3189, 6451, 2330, 2303, 2454`
- Window: **2024-01-02 → 2026-07-22**
- Pre-registered variants: 12

## Verdict: OOS≥70% + stable？ **NO**

- IS champion: `idx_tod_dry_0p5_L10`
- Champion IS: **81.25%** (n=208)
- Champion OOS: **71.76%** (n=131)
- Best OOS in grid (**exploratory · not claim**): `idx_tod_dry_0p7_L10` → **74.02%** (n=204)

## Baselines

- `fade_near_ext` OOS: **64.02%** (n=1651)
- `fade_idx_or_inside` OOS: **70.66%** (n=634)

## Prior peek note（session dry-up）

- Prior exploratory `fade_dryup_05` OOS≈68.09% (n≈796) was **best-OOS**, not IS champion (`fade_ud_agree` IS-pick OOS≈59.8%) — peek risk.
- E4 uses TOD median RVOL + IS-locked pick to close that gap.

## Gates（IS champion OOS）

- `gate_oos_hit_ge70_n500`: **False**
- `gate_name_stability`: **False** (0/7 = 0.0)
- `gate_no_megacap_dom`: **True** (max share 0.244 · 2454)

## Pool leaderboard（OOS directed 排序）

|variant|family|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|
|---|---|---:|---:|---:|---:|---:|
|idx_tod_dry_0p7_L10|idx_tod|79.82|337|74.02|204|24.02|
|idx_tod_dry_1p0_L20|idx_tod|79.68|507|71.9|274|21.9|
|idx_tod_dry_0p5_L10|idx_tod|81.25|208|71.76|131|21.76|
|idx_tod_dry_0p7_L20|idx_tod|80.06|331|71.36|206|21.36|
|baseline_fade_idx_or_inside|baseline|71.57|1333|70.66|634|20.66|
|idx_tod_dry_0p5_L20|idx_tod|82.56|195|70.37|135|20.37|
|fade_tod_dry_0p7_L10|fade_tod|75.15|676|64.41|458|14.41|
|fade_tod_dry_1p0_L20|fade_tod|75.53|1030|64.26|610|14.26|
|baseline_fade_near_ext|baseline|68.56|3158|64.02|1651|14.02|
|fade_tod_dry_0p5_L10|fade_tod|75.97|412|63.25|302|13.25|
|fade_tod_dry_0p7_L20|fade_tod|75.63|636|62.87|439|12.87|
|fade_tod_dry_0p5_L20|fade_tod|77.14|385|62.0|300|12.0|

## IS champion per-stock OOS · `idx_tod_dry_0p5_L10`

|sid|OOS hit%|OOS n|E[signed]%|
|---|---:|---:|---:|
|2330|86.36|22|0.2834|
|2454|75.0|32|0.1444|
|3189|71.43|7|0.4129|
|2327|70.59|17|0.2067|
|2303|70.0|20|0.2694|
|8046|66.67|9|0.2988|
|6451|58.33|24|0.2257|

## Sensitivity · flat_eps=0.10%

- Same IS champion OOS @ flat 0.10%: **71.76%** (n=131)

## Honest note

IS 冠軍 `idx_tod_dry_0p5_L10` OOS=71.76% (n=131)；格子最佳 OOS（exploratory）`idx_tod_dry_0p7_L10`=74.02% (n=204)。 裸 fade OOS=64.02%；fade_idx_or_inside OOS=70.66%。 閘門只認 IS 冠軍之一次 OOS；不採 best-OOS 當宣稱（修 dry-up peek）。

## Do not

- 未過閘寫入 `strategy.yaml` / Order live
- 用格子最佳 OOS 當宣稱（即先前 dry-up peek）

Generated: `2026-07-24T11:48:06`
Runner: `scripts/research/run_ta_30m_track_e4_tod_rvol.py`
