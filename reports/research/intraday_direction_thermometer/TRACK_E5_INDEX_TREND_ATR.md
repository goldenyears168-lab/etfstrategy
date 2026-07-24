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
- Helps close 0050↔IX0001 gap? **NO — best IX OOS `fade_or_inside_and_vwap_mr` at 70.92% <70 or n<500; gap explained by OR-state disagreement, not fixed by VWAP/hold filters in this grid**

## Gates（0050 · IS champion OOS）

- `gate_oos_hit_ge70_n500`: **False**
- `gate_name_stability`: **False** (1/7 = 0.143)
- `gate_no_megacap_dom`: **True** (max share 0.253 · 2454)

## Leaderboard · bench=0050（OOS hit 排序）

|variant|IS hit%|IS n|OOS hit%|OOS n|MAE% med|MAE/ATR med|E[signed]%|
|---|---:|---:|---:|---:|---:|---:|---:|
|fade_or_inside_flat_vwap|71.59|1292|71.26|609|0.0|0.0|0.1643|
|fade_or_inside_and_vwap_mr|72.48|585|70.7|273|0.0|0.0|0.1971|
|fade_idx_or_inside|71.57|1333|70.66|634|0.0|0.0|0.1662|
|fade_skip_idx_trend|71.51|1334|70.55|635|0.0|0.0|0.1649|
|fade_skip_or_hold_k2|70.11|1502|70.21|678|0.0|0.0|0.1599|
|fade_skip_or_hold_k3|68.58|1636|69.61|760|0.0|0.0|0.1488|
|fade_idx_vwap_mr|69.84|829|68.45|431|0.0|0.0|0.2097|
|baseline_fade_near_ext|68.56|3158|64.02|1651|0.1701|0.0287|0.1403|

## Leaderboard · bench=IX0001（OOS-only · no IS claim）

|variant|OOS hit%|OOS n|MAE% med|MAE/ATR med|E[signed]%|
|---|---:|---:|---:|---:|---:|
|fade_or_inside_and_vwap_mr|70.92|282|0.0|0.0|0.2768|
|fade_idx_vwap_mr|68.38|370|0.0|0.0|0.2346|
|fade_or_inside_flat_vwap|67.83|687|0.0|0.0|0.1591|
|fade_skip_or_hold_k2|67.41|902|0.0|0.0|0.1543|
|fade_idx_or_inside|66.86|851|0.0|0.0|0.1562|
|fade_skip_idx_trend|66.78|852|0.0|0.0|0.1552|
|fade_skip_or_hold_k3|66.49|970|0.0|0.0|0.1305|
|baseline_fade_near_ext|65.36|1536|0.1297|0.0187|0.1648|

## MAE / ATR findings

Adverse excursion over the same H=6 path (not a stop engine).
- **0050 · fade_idx_or_inside**: MAE% med=0.0 · mean=0.4179; MAE/ATR med=0.0 · mean=0.0846; E[signed]%=0.1662 (n_dir=634)
- **IX0001 · fade_idx_or_inside**: MAE% med=0.0 · mean=0.4581; MAE/ATR med=0.0 · mean=0.0847; E[signed]%=0.1562 (n_dir=851)
- **0050 · baseline_fade**: MAE% med=0.1701 · mean=0.4883; MAE/ATR med=0.0287 · mean=0.097; E[signed]%=0.1403 (n_dir=1651)
- **IX0001 · baseline_fade**: MAE% med=0.1297 · mean=0.4833; MAE/ATR med=0.0187 · mean=0.0916; E[signed]%=0.1648 (n_dir=1536)
- Disagreement `0050_inside_ix_out` MAE/ATR med=0.0 vs agree_inside 0.0 — MAE not clearly worse; gap is mostly hit-rate dilution.

## 0050 vs IX0001 gap analysis

On OOS days with **both** benches present, `fade_near_ext` signals where **both** mark OR-inside hit **73.58%** (n=492). Where **0050 is inside but IX0001 already broke OR** (`0050_inside_ix_out`): hit **69.86%** (n=73). That disagreement bucket is the mechanical source of the ~3.8pp proxy gap (0050 or_inside 70.66% vs IX 66.86%): 0050 still labels rotation while true TAIEX has already trend-broken. Extra VWAP / OR-hold filters did not lift IX0001 pool OOS to ≥70% with n≥500 under shared gates.

### Disagreement buckets（OOS · among `fade_near_ext`）

|bucket|hit%|n|MAE% med|MAE/ATR med|
|---|---:|---:|---:|---:|
|agree_inside|73.58|492|0.0|0.0|
|agree_outside|62.75|612|0.2218|0.0434|
|0050_inside_ix_out|69.86|73|0.0|0.0|
|ix_inside_0050_out|57.66|359|0.2894|0.0447|
|only_0050_day|50.72|69|0.3279|0.1069|
|only_ix_day|None|0|None|None|

## Interpretation

E5 tests index **trend** context beyond OR-inside: VWAP mean-reversion side, OR-break hold ≥K bars, and combinations. On the **0050** proxy, `fade_idx_or_inside` remains the IS champion (70.7% OOS). Stricter trend ANDs mostly thin n without a clean IS→OOS lift past the frozen champion. On **IX0001** (OOS-only), or_inside stays ~66.86% and no pre-registered trend filter clears shared ≥70%+stability gates. MAE/ATR shows fade adversity is a fraction of daily ATR over 30m — useful risk context, not a substitute for hit-rate gates. **Do not** update Live ta_30m_bias or Order from E5.

## Claim

- `oos_ge_70_stable_0050`: **False**
- `oos_ge_70_stable_ix0001`: **False**
- `live_bias_updated`: **False**
- `order_adopted`: **False**

## Artifacts

- JSON: `reports/research/intraday_direction_thermometer/track_e5_index_trend_atr.json`
- MD: `reports/research/intraday_direction_thermometer/TRACK_E5_INDEX_TREND_ATR.md`
- Runner: `scripts/research/run_ta_30m_track_e5_index_trend_atr.py`

