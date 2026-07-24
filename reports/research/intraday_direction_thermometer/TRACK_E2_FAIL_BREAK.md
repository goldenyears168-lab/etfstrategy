# Track E2 · Explicit failed-OR-break fade · ~30m

Research only · **未採納** · not Order / not `strategy.yaml`

## Hypothesis

Event-defined fail-break (wick beyond OR → close back inside within N, weak poke volume preferred) should beat continuous `fade_near_ext` and match or lift `fade_idx_or_inside` (~70.7% prior on 0050 proxy).

## Metric / freeze

- **Directed hit (~30m)**: `temp∈{±1}` and `|fwd|≥0.0005`; hit iff `sign(temp)==sign(fwd)`.
- Forward: `close[i+6]/close[i]-1` @ 5m.
- PIT: stock (+bench for idx filter) uses completed same-session bars `≤ i`.
- IS / OOS: IS `≤ 2025-09-30`; **fail_break family** champion on IS only; one OOS claim read.
- Universe (ex `0050` proxy): `2327, 8046, 3189, 6451, 2330, 2303, 2454`.
- Pre-registered variants: 11.
- Window: **2024-01-02 → 2026-07-22**

## Verdict: OOS≥70% + stable？ **NO**

- IS champion (fail_break*): `fb_n1`
- Champion IS hit: **36.7%** (n=4480)
- Champion OOS hit: **37.21%** (n=3123)
- Baseline `fade_near_ext` OOS: **64.02%** (n=1651)
- Baseline `fade_idx_or_inside` OOS: **69.14%** (n=687)
- Beats fade_idx_or_inside on OOS? **False**
- Helps toward 70%? **NO — fail-break IS champ does not clear OOS≥70%+stability**

## Gates（IS-locked fail-break champion OOS）

- `gate_oos_hit_ge70_n500`: **False**
- `gate_name_stability`: **False** (0/7 = 0.0)
- `gate_no_megacap_dom`: **True** (max share 0.207 · 2330)

## Pool leaderboard（OOS directed 排序）

|variant|family|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|Δ vs fade|Δ vs idx_or|
|---|---|---:|---:|---:|---:|---:|---:|---:|
|fade_idx_or_inside|baseline|71.55|1332|69.14|687|19.14|5.12|0.0|
|baseline_fade_near_ext|baseline|68.56|3158|64.02|1651|14.02|0.0|-5.12|
|fb_n2_weak1p0_and_idx_or_inside|fail_break|36.15|1740|41.22|1128|-8.78|-22.8|-27.92|
|fb_n3_weak1p0_and_idx_or_inside|fail_break|36.15|1740|41.22|1128|-8.78|-22.8|-27.92|
|fb_n2_weak1p0_vwap_side|fail_break|35.99|3265|39.19|1891|-10.81|-24.83|-29.95|
|fb_n2_weak1p0|fail_break|35.53|3574|37.8|2394|-12.2|-26.22|-31.34|
|fb_n2_weak1p0_beyond_mid|fail_break|35.56|3571|37.73|2388|-12.27|-26.29|-31.41|
|fb_n2_weak1p0_midday|fail_break|35.83|2724|37.68|1765|-12.32|-26.34|-31.46|
|fb_n1|fail_break|36.7|4480|37.21|3123|-12.79|-26.81|-31.93|
|fb_n2|fail_break|36.7|4480|37.21|3123|-12.79|-26.81|-31.93|
|fb_n3|fail_break|36.7|4480|37.21|3123|-12.79|-26.81|-31.93|

## IS champion per-stock OOS · `fb_n1`

|sid|OOS hit%|OOS n|E[signed]%|
|---|---:|---:|---:|
|3189|47.66|235|-0.183|
|2303|43.33|547|-0.1437|
|8046|43.17|410|-0.186|
|6451|39.62|265|-0.1854|
|2327|38.37|516|-0.1685|
|2454|35.71|504|-0.1901|
|2330|23.68|646|-0.2371|

## Interpretation

IS-locked fail-break champion `fb_n1` → OOS directed hit 37.21% (n=3123). Baselines: fade_near_ext OOS 64.02% (n=1651); fade_idx_or_inside OOS 69.14% (n=687). Explicit poke-and-fail is event-sparse relative to continuous near-extreme fade; weak-vol / mid / VWAP / idx AND filters further thin n. Research only — no Live 70% claim, no Order.

## Artifacts

- JSON: `reports/research/intraday_direction_thermometer/track_e2_fail_break.json`
- MD: `reports/research/intraday_direction_thermometer/TRACK_E2_FAIL_BREAK.md`
- Runner: `scripts/research/run_ta_30m_track_e2_fail_break.py`
- Helper: `or_fail_break_temp` in `src/research/intraday_direction_thermometer.py`

