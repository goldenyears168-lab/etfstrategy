# Track · 5m EMA + rolling beta residual · ~30m

Research only · **未採納** · not Order / not `strategy.yaml`

## Question

Do **5m EMA9/21** and **rolling beta vs 0050 + residual** raise fade30 OOS directed hit to ≥60% with multi-stock stability? (Features absent from prior 30m path; daily WMA was killed in RRG track.)

## Metric / freeze

- **Directed hit (~30m)**: `temp∈{±1}` and `|fwd|≥0.0005`; hit iff `sign(temp)==sign(fwd)`.
- Forward: `close[i+6]/close[i]-1` @ 5m.
- PIT: EMA/beta/fade from completed same-session bars `≤ i` only.
- IS / OOS: IS `≤ 2025-09-30`; champion on **IS only**; claim on OOS.
- Universe (ex `0050`): `2327, 8046, 3189, 6451, 2330, 2303, 2454`.
- Pre-registered variants: 16.
- Window: **2024-01-02 → 2026-07-22**
- Gate this pass: OOS≥**60%** · n≥500 · name stab ≥70% names @≥55% (n≥50).

## Verdict: new EMA/beta rule clears OOS≥60% + stable？ **NO**

- **Best claimable (n≥500)**: `fade_idx_or_inside` OOS **70.66%** (n=634); gates **True** — reproduced prior 大盤 OR-inside filter; **not** a new EMA/beta lift.
- IS champion (this track): `fade_idx_and_ema_ext`
- Champion IS hit: **73.66%** (n=805)
- Champion OOS hit: **70.3%** (n=431)
- Best OOS exploratory: `fade_idx_and_ema_flat` → **93.02%** (n=86)
- Best OOS with n≥500: `fade_idx_or_inside` → **70.66%** (n=634)
- Did EMA help? **NO (-1.19pp vs base 64.02%)**
- Did beta/residual help? **NO (-2.21pp vs base 64.02%)**
- Did 大盤 (idx OR) help? **YES (+6.64pp vs base 64.02%)**

### Note on thin-n peaks

`fade_idx_and_ema_flat` / `fade_ema_flat_cross` can print OOS hit ≫70% but **n≪500** and megacap-dominated — **not claimable** (same class as E4 TOD dry-up). Stacking EMA/beta on idx **thins n** below 500 and hurts name stability vs bare `fade_idx_or_inside`.

## Gates（IS champion OOS）

- `gate_oos_hit_ge60_n500`: **False**
- `gate_name_stability`: **False** (3/7 = 0.429)
- `gate_no_megacap_dom`: **True** (max share 0.227 · 2330)

## Pool leaderboard（OOS directed 排序）

|variant|family|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|Δ vs fade|
|---|---|---:|---:|---:|---:|---:|---:|
|fade_idx_and_ema_flat|fusion|82.3|243|93.02|86|43.02|29.0|
|fade_ema_flat_cross|fusion|78.33|406|80.27|147|30.27|16.25|
|fade_idx_or_inside|baseline|71.57|1333|70.66|634|20.66|6.64|
|fade_idx_and_ema_ext|fusion|73.66|805|70.3|431|20.3|6.28|
|fade_idx_and_resid_ext|fusion|77.19|456|70.04|277|20.04|6.02|
|fade_high_beta|fusion|70.36|631|65.09|275|15.09|1.07|
|baseline_fade_near_ext|baseline|68.56|3158|64.02|1651|14.02|0.0|
|fade_ema_ext_slow|fusion|70.15|2050|62.83|1243|12.83|-1.19|
|fade_resid_agree_ext|fusion|71.84|1172|61.81|872|11.81|-2.21|
|fade_ema_cross_against|fusion|68.13|1644|60.49|1096|10.49|-3.53|
|fade_low_beta|fusion|71.14|797|60.18|683|10.18|-3.84|
|baseline_ema_cross|baseline|49.48|17788|50.04|20813|0.04|-13.98|
|fade_resid_against|fusion|63.27|49|47.62|21|-2.38|-16.4|
|baseline_resid_sign|baseline|40.06|18260|45.25|20451|-4.75|-18.77|
|baseline_price_vs_ema9|baseline|37.61|19568|43.13|22022|-6.87|-20.89|
|fade_ema_cross_agree|fusion|None|0|None|0|None|None|

## IS champion per-stock OOS · `fade_idx_and_ema_ext`

|sid|OOS hit%|OOS n|E[signed]%|
|---|---:|---:|---:|
|2330|90.82|98|0.3321|
|8046|77.78|18|0.5261|
|2327|77.36|53|0.2543|
|2454|76.92|78|0.2509|
|2303|61.36|44|0.2083|
|3189|52.38|63|-0.3478|
|6451|50.65|77|-0.0646|

## Leave-one-stock-out OOS · `fade_idx_and_ema_ext`

|drop|OOS hit%|OOS n|
|---|---:|---:|
|2330|64.26|333|
|2454|68.84|353|
|2327|69.31|378|
|8046|69.98|413|
|2303|71.32|387|
|3189|73.37|368|
|6451|74.58|354|

## Interpretation

Standalone EMA / residual signs are expected ~coin (price_vs_ema9 OOS=43.13%; resid OOS=45.25%). Fade∧EMA-ext OOS=62.83% · fade∧resid-ext OOS=61.81% vs unfiltered fade OOS=64.02%. Prior 大盤 filter `fade_idx_or_inside` OOS=70.66% remains the strong lift. Stacking EMA/residual on idx: idx∧ema_ext OOS=70.3% (NO (-0.36pp vs base 70.66%)); idx∧resid OOS=70.04% (NO (-0.62pp vs base 70.66%)). IS champion `fade_idx_and_ema_ext` OOS=70.3% n=431; gates_pass=False; leave-one-out min OOS=64.26%. Do not Order-graduate; Live observe unchanged unless a fresh holdout confirms a rule that beats `fade_idx_or_inside` on gates.

## Claim

- `oos_ge_60_stable_is_champ`: **False**
- `oos_ge_60_stable_best_thick`: **True** (`fade_idx_or_inside`)
- `live_bias_updated`: **False**
- `order_adopted`: **False**

## Artifacts

- JSON: `reports/research/intraday_direction_thermometer/track_beta_ema_30m.json`
- MD: `reports/research/intraday_direction_thermometer/TRACK_BETA_EMA.md`
- Runner: `scripts/research/run_ta_30m_track_beta_ema.py`

