# Track E3 · VWAP 2σ + 拒絕K + ADX／寬OR skip ∧ fade_idx_or_inside

Research only · **未採納** · not Order / not `strategy.yaml`

## Hypothesis

- Midday fade at session VWAP ±2σ with rejection candle.
- Skip when ADX(14)@5m > 25 or OR width > 0.75× ATR(14).
- AND with **idx OR-inside** (0050) from `fade_idx_or_inside`.
- Note: `fade_near_ext` ∩ VWAP-rejection is nearly empty → primary `e3_full` uses rejection∧skip∧idx-OR, not fade_near_ext coincidence.

## Metric / freeze

- **Directed hit (~30m)**: `temp∈{±1}` and `|fwd|≥0.0005`; hit iff `sign(temp)==sign(fwd)`.
- Forward: `close[i+6]/close[i]-1` @ 5m.
- PIT: VWAP/σ/ADX/ATR/OR/rejection use completed same-session bars `≤ i`.
- IS / OOS: IS `≤ 2025-09-30`; IS-lock once; claim on OOS.
- Universe (ex `0050`): `2327, 8046, 3189, 6451, 2330, 2303, 2454`.
- Bench for OR-inside: **`0050` 5m**.
- Window: **2024-01-02 → 2026-07-22**
- Pre-registered: `baseline_fade_near_ext, fade_idx_or_inside, vwap2s_rej, vwap2s_rej_skip, e3_vwap_idx, e3_full, fade_idx_skip, fade_idx_at_2s`.

## Verdict: OOS≥70% + stable？ **NO**

- Pre-registered E3 full (`e3_full`) OOS: **41.61%** (n=1233)
- E3 full IS: **38.26%** (n=1448)
- Baseline fade OOS: **64.02%** (n=1651)
- Baseline `fade_idx_or_inside` OOS: **70.66%** (n=634)
- Δ vs fade (OOS): **-22.41** pp
- Δ vs idx_or_inside (OOS): **-29.05** pp
- IS champion (grid): `fade_idx_at_2s` · IS 75.11% · OOS 73.77%
- Helps toward 70%? **NO — e3_full OOS 41.61% < idx 70.66% (Δ-29.05pp); stack thins or hurts**

## Gates（`e3_full` OOS）

- `gate_oos_hit_ge70_n500`: **False**
- `gate_name_stability`: **False** (1/7 = 0.143)
- `gate_no_megacap_dom`: **True** (max share 0.314 · 2330)

## Pool leaderboard（OOS directed 排序）

|variant|family|IS hit%|IS n|OOS hit%|OOS n|Δ vs fade|Δ vs idx|
|---|---|---:|---:|---:|---:|---:|---:|
|fade_idx_at_2s|e3|75.11|900|73.77|427|9.75|3.11|
|fade_idx_skip|e3|69.23|858|71.07|363|7.05|0.41|
|fade_idx_or_inside|baseline|71.57|1333|70.66|634|6.64|0.0|
|baseline_fade_near_ext|baseline|68.56|3158|64.02|1651|0.0|-6.64|
|vwap2s_rej|e3|37.04|3761|42.59|3750|-21.43|-28.07|
|e3_vwap_idx|e3|37.12|1972|41.82|1913|-22.2|-28.84|
|e3_full|e3|38.26|1448|41.61|1233|-22.41|-29.05|
|vwap2s_rej_skip|e3|39.36|2561|41.45|2222|-22.57|-29.21|

## `e3_full` per-stock OOS

|sid|OOS hit%|OOS n|E[signed]%|
|---|---:|---:|---:|
|6451|55.74|61|0.113|
|3189|52.94|51|-0.2441|
|8046|46.34|123|-0.0812|
|2303|43.75|176|-0.1304|
|2330|42.89|387|-0.0682|
|2327|37.86|206|-0.203|
|2454|32.31|229|-0.2948|

## Interpretation

Unfiltered fade OOS 64.02% (n=1651); `fade_idx_or_inside` OOS 70.66% (n=634). Pre-registered `e3_full` (VWAP±2σ rejection ∧ ADX/OR skip ∧ 0050 OR-inside) OOS 41.61% (n=1233; IS 38.26% n=1448). Δ vs fade -22.41pp · Δ vs idx -29.05pp. Standalone VWAP-rejection is ~coin or worse; idx-OR gate helps fade30 more than VWAP-band structure. `fade_near_ext` ∩ rejection is nearly empty (different extremes) — documented, not forced. Research only — do not Order-graduate; Live observe unchanged.

## Artifacts

- JSON: `reports/research/intraday_direction_thermometer/track_e3_vwap_bands.json`
- MD: `reports/research/intraday_direction_thermometer/TRACK_E3_VWAP_BANDS.md`
- Runner: `scripts/research/run_ta_30m_track_e3_vwap_bands.py`
