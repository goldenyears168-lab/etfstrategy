# TA 30m multi-factor bias · PIT directed-hit evaluation

Research only · **未採納** · not Order / not `strategy.yaml`

## Metric definition（預先鎖定）

- **Primary directed hit (~30m)**: samples with `temp ∈ {±1}` and `|fwd| ≥ 0.0005` (=0.05%), hit iff `sign(temp) == sign(fwd)`.
- **Forward**: `close[i+6] / close[i] - 1`（≈30 分；同日 5m）。
- **PIT**: 因子僅用當日已完成 5m bars `≤ i`。
- **Null baselines**: coin-flip 50%；always-long；`baseline_mom30`；`baseline_vwap`；`baseline_live_confluence`；`baseline_fade_near_ext`。
- **IS / OOS**: IS `trade_date ≤ 2025-09-30`；OOS `>`。 調參／選冠軍只看 IS；**主閘門只認 OOS**。
- **Gates**: OOS hit ≥ **70%** 且 n≥500；≥70% 個股 OOS hit≥55% 且 n≥50；單一名稱 ≤40% pool n。

- Window: **2024-01-02 → 2026-07-22**
- Universe: `2327, 8046, 3189, 6451, 2330, 2303, 2454, 0050`
- Pre-registered variants: 22

## Verdict: OOS≥70% + stable？ **NO**

- IS champion: `baseline_fade_near_ext`
- Champion IS hit: **66.86%** (n=3467)
- Champion OOS hit: **62.13%** (n=1846)
- Best OOS among grid (exploratory, not claim): `baseline_fade_near_ext` → **62.13%** (n=1846)
- Live bias updated? **False**

## Gates detail（IS champion OOS）

- `gate_oos_hit_ge70_n500`: **False**
- `gate_name_stability`: **False** (5/8 = 0.625)
- `gate_no_megacap_dom`: **True** (max share 0.208 · 2330)

## Null / baseline pool（OOS）

|variant|OOS hit%|OOS n|vs coin|vs long|
|---|---:|---:|---:|---:|
|baseline_fade_near_ext|62.13|1846|12.13|11.26|
|baseline_or_break|50.36|20888|0.36|0.79|
|baseline_vwap|46.06|43230|-3.94|-2.16|
|baseline_live_confluence|45.23|41543|-4.77|-2.9|
|baseline_mom30|45.17|38807|-4.83|-2.83|
|baseline_short_mom|45.04|38304|-4.96|-2.56|

## Pool leaderboard · all pre-registered（OOS directed 排序）

|variant|family|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|
|---|---|---:|---:|---:|---:|---:|
|baseline_fade_near_ext|baseline|66.86|3467|62.13|1846|12.13|
|fade_against_mom|fusion|58.43|9572|59.48|3939|9.48|
|and_fade_vwap_mr|fusion|55.88|15438|57.09|5880|7.09|
|baseline_or_break|baseline|50.35|27381|50.36|20888|0.36|
|and_mom_vwap_or|fusion|45.43|13222|46.15|10470|-3.85|
|baseline_vwap|baseline|48.28|51403|46.06|43230|-3.94|
|baseline_live_confluence|baseline|46.38|53746|45.23|41543|-4.77|
|score2_mom_vwap_or|fusion|46.91|36856|45.2|30267|-4.8|
|baseline_mom30|baseline|43.79|40930|45.17|38807|-4.83|
|score_fade_short_mr|fusion|44.09|28853|45.13|24879|-4.87|
|baseline_short_mom|baseline|42.1|43812|45.04|38304|-4.96|
|score2_four_factors|fusion|44.57|40685|44.42|32435|-5.58|
|or_agree_mom_vwap_short|fusion|44.31|51275|44.24|36689|-5.76|
|and_mom_vwap_mag030|fusion|42.78|20021|43.92|20204|-6.08|
|and_mom_vwap_mag050|fusion|44.64|9517|43.8|14098|-6.2|
|and_mom_vwap|fusion|44.54|27065|43.69|25256|-6.31|
|score3_mom_vwap_short_or|fusion|43.78|23357|43.58|20311|-6.42|
|and_mom_vwap_vol|fusion|45.26|10901|43.57|9444|-6.43|
|score2_mag030|fusion|41.1|20460|43.45|19302|-6.55|
|score2_mom_vwap_short|fusion|42.92|34374|43.14|28108|-6.86|
|and_mom_vwap_short|fusion|42.76|17328|42.64|16896|-7.36|
|and_mom_vwap_midday|fusion|43.87|18269|41.27|16396|-8.73|

## IS champion per-stock OOS · `baseline_fade_near_ext`

|sid|OOS hit%|OOS n|E[signed]%|
|---|---:|---:|---:|
|2330|78.91|384|0.2275|
|2327|74.07|162|0.3179|
|2454|69.86|345|0.2452|
|8046|67.9|81|0.3533|
|6451|55.31|273|0.1213|
|3189|47.98|173|-0.1461|
|0050|46.15|195|-0.0756|
|2303|44.64|233|-0.1216|

## Why 70% is hard（honest）

IS 冠軍 `baseline_fade_near_ext` OOS=62.13% (n=1846)；格子內最佳 OOS 同為 **62.13%**。
動能／VWAP／OR／score／AND 合流 OOS 多在 **41–50%**（Live confluence≈45%），
低於或貼近擲幣；只有 **貼近日極值的均值回歸淡化** 能穩定到低六十。
個股異質：2330/2327/2454 偏強，3189/2303/0050 偏弱（穩定性閘 5/8=62.5%＜70%）。
再疊 AND／幅度閘並未把命中抬到 70%，反而常降到低四十。
在不弱化閘門、不事後擴格挑 OOS 的前提下，**pool OOS≥70% 本輪不可達**。

## Sensitivity · flat_eps=0.10%

- Same IS champion OOS @ flat 0.10%: **62.38%** (n=1805)

## Do not

- 未過閘寫入 `strategy.yaml` / Order live
- 把 Live `ta_30m_bias` 說成 OOS≥70%
- 事後擴格子再挑 OOS 冠軍當宣稱

Generated: `2026-07-24T10:21:15`
