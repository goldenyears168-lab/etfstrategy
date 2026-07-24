# Track E5 · Index trend filter + MAE/ATR · ~30m

Research only · **未採納** · not Order / not `strategy.yaml`

## Question

Does an index **trend** filter beyond `fade_idx_or_inside` push **IX0001** OOS directed hit to ≥70%? What do MAE / ATR-normalized adverse excursions say about fade risk? Why 0050≈70.7% vs IX0001≈66.9%?

## Metric / freeze

- **Directed hit (~30m)**: `temp∈{±1}` and `|fwd|≥0.0005`; hit iff `sign(temp)==sign(fwd)`.
- Forward: `close[i+6]/close[i]-1` @ 5m.
- PIT: stock+bench factors use completed same-session bars `≤ i`; ATR14% ends **T−1**.
- IS / OOS: IS `≤ 2025-09-30`; champion on **IS** for 0050; IX0001 is OOS-only sensitivity (0 IS days).
- Universe (ex benches): `2327, 8046, 3189, 6451, 2330, 2303, 2454`.
- Pre-registered variants: 8.
- Window: **2024-01-02 → 2026-07-22**

## Verdict: trend filter → IX0001 ≥70%？ **NO**

- 0050 IS champion: `fade_or_inside_and_vwap_mr` → OOS **70.7%** (n=273) · gates **FAIL**
- IX0001 best OOS (exploratory / no IS): `fade_or_inside_and_vwap_mr` → **70.92%** (n=282)
- IX0001 `fade_idx_or_inside` OOS: **66.86%** (n=851)
- Helps close 0050↔IX0001 gap? **NO — `fade_or_inside_and_vwap_mr` raw IX OOS 70.92% but n=282<500 and name-stability fails; cannot claim ≥70% stable**
- Prior stable ref `fade_idx_or_inside` @0050: **70.66%** (n=634; gates **PASS**)

## Gates（0050 · IS champion OOS）

- `gate_oos_hit_ge70_n500`: **False**
- `gate_name_stability`: **False** (1/7 = 0.143)
- `gate_no_megacap_dom`: **True** (max share 0.253 · 2454)

## Leaderboard · bench=0050（OOS hit 排序）

|variant|IS hit%|IS n|OOS hit%|OOS n|MAE% mean|MAE% p75|MAE/ATR mean|E[signed]%|
|---|---:|---:|---:|---:|---:|---:|---:|---:|
|fade_or_inside_flat_vwap|71.59|1292|71.26|609|0.4046|0.5543|0.0826|0.1643|
|fade_or_inside_and_vwap_mr|72.48|585|70.7|273|0.3527|0.5543|0.0778|0.1971|
|fade_idx_or_inside|71.57|1333|70.66|634|0.4179|0.579|0.0846|0.1662|
|fade_skip_idx_trend|71.51|1334|70.55|635|0.4189|0.5865|0.0849|0.1649|
|fade_skip_or_hold_k2|70.11|1502|70.21|678|0.4212|0.5873|0.0864|0.1599|
|fade_skip_or_hold_k3|68.58|1636|69.61|760|0.4353|0.6173|0.0883|0.1488|
|fade_idx_vwap_mr|69.84|829|68.45|431|0.3325|0.4975|0.0735|0.2097|
|baseline_fade_near_ext|68.56|3158|64.02|1651|0.4883|0.692|0.097|0.1403|

## Leaderboard · bench=IX0001（OOS-only · no IS claim）

|variant|OOS hit%|OOS n|MAE% mean|MAE% p75|MAE/ATR mean|E[signed]%|
|---|---:|---:|---:|---:|---:|---:|
|fade_or_inside_and_vwap_mr|70.92|282|0.3038|0.5496|0.0677|0.2768|
|fade_idx_vwap_mr|68.38|370|0.3142|0.559|0.0714|0.2346|
|fade_or_inside_flat_vwap|67.83|687|0.4265|0.6299|0.081|0.1591|
|fade_skip_or_hold_k2|67.41|902|0.4539|0.6344|0.0849|0.1543|
|fade_idx_or_inside|66.86|851|0.4581|0.641|0.0847|0.1562|
|fade_skip_idx_trend|66.78|852|0.4588|0.641|0.0851|0.1552|
|fade_skip_or_hold_k3|66.49|970|0.4715|0.649|0.088|0.1305|
|baseline_fade_near_ext|65.36|1536|0.4833|0.6757|0.0916|0.1648|

## MAE / ATR findings

Adverse excursion over the same H=6 path (research risk context; not Order stops). Median often ≈0 because ≥50% of directed fades never print a worse extreme than entry within 30m — use **mean / p75 / miss-MAE**.
- **0050 · fade_idx_or_inside**: MAE% mean=0.4179 · p75=0.579 · zero_frac=0.557; MAE/ATR mean=0.0846 · p75=0.1223; miss MAE% mean=1.1469 (miss MAE/ATR=0.2364); E[signed]%=0.1662 (n_dir=634)
- **IX0001 · fade_idx_or_inside**: MAE% mean=0.4581 · p75=0.641 · zero_frac=0.511; MAE/ATR mean=0.0847 · p75=0.1255; miss MAE% mean=1.1249 (miss MAE/ATR=0.21); E[signed]%=0.1562 (n_dir=851)
- **0050 · baseline_fade**: MAE% mean=0.4883 · p75=0.692 · zero_frac=0.487; MAE/ATR mean=0.097 · p75=0.1479; miss MAE% mean=1.1278 (miss MAE/ATR=0.2265); E[signed]%=0.1403 (n_dir=1651)
- **IX0001 · baseline_fade**: MAE% mean=0.4833 · p75=0.6757 · zero_frac=0.495; MAE/ATR mean=0.0916 · p75=0.1404; miss MAE% mean=1.1457 (miss MAE/ATR=0.2181); E[signed]%=0.1648 (n_dir=1536)
- Bucket `ix_inside_0050_out` MAE/ATR mean=0.0911 vs agree_inside 0.0801 — worse adverse when IX alone keeps the inside label.

## 0050 vs IX0001 gap analysis

Decompose OOS `fade_near_ext` where **both** benches exist: **agree_inside** hit **73.58%** (n=492). The large dilution bucket is **`ix_inside_0050_out`** (IX still OR-inside, 0050 already outside): hit **57.66%** (n=359). IX `fade_idx_or_inside` ≈ agree_inside + that bucket → 66.86% (n=851). Conversely `0050_inside_ix_out` is small (n=73, hit 69.86%). So the ~3.8pp gap (0050 70.66% vs IX 66.86%) is mostly **IX keeping low-quality ‘index still inside’ signals after the ETF proxy has already broken OR**, not 0050 falsely keeping rotation. VWAP / OR-hold ANDs raise raw IX hit on thin n (e.g. `fade_or_inside_and_vwap_mr` 70.92% n=282) but **fail n≥500 + name stability**.

### Disagreement buckets（OOS · among `fade_near_ext`）

|bucket|hit%|n|MAE% mean|MAE/ATR mean|
|---|---:|---:|---:|---:|
|agree_inside|73.58|492|0.4237|0.0801|
|agree_outside|62.75|612|0.5365|0.1051|
|0050_inside_ix_out|69.86|73|0.3312|0.0581|
|ix_inside_0050_out|57.66|359|0.5052|0.0911|
|only_0050_day|50.72|69|0.4687|0.1451|
|only_ix_day|None|0|None|None|

## Interpretation

E5 tests index **trend** context beyond OR-inside: VWAP mean-reversion side, OR-break hold ≥K bars, and combinations. IS-max on 0050 is `fade_or_inside_and_vwap_mr` (OOS 70.7%, n=273) but **fails** n≥500 / name stability. Prior stable reference `fade_idx_or_inside` stays 70.66% OOS (n=634; gates PASS). On **IX0001**, or_inside=66.86% (n=851); stricter ANDs do not clear shared gates. Gap vs 0050 is explained by **`ix_inside_0050_out`** dilution. MAE/ATR: mean adverse ≈0.4–0.5% (≈0.08–0.10×ATR14) over 30m; misses worse than hits. **Do not** update Live / Order from E5.

## Claim

- `oos_ge_70_stable_0050_is_champ`: **False**
- `oos_ge_70_stable_0050_or_inside`: **True**
- `oos_ge_70_stable_ix0001`: **False**
- `live_bias_updated`: **False**
- `order_adopted`: **False**

## Artifacts

- JSON: `reports/research/intraday_direction_thermometer/track_e5_index_trend_atr.json`
- MD: `reports/research/intraday_direction_thermometer/TRACK_E5_INDEX_TREND_ATR.md`
- Runner: `scripts/research/run_ta_30m_track_e5_index_trend_atr.py`

