# Track · 距離大盤的比例 / relative-to-market · ~30m

Research only · **未採納** · not Order / not `strategy.yaml`

## Data reality

- Intraday bench: **`0050` 5m** — 611d (2024-01-02→2026-07-22); IS=418 · OOS=193.
- True TAIEX **`IX0001` 5m**: 184d (2025-10-15→2026-07-21); **IS days=0** → cannot drive IS selection; OOS-only sensitivity.
- Daily TAIEX **`IX0001`**: 4280 rows (2015-01-05→2026-07-20) → overnight RS (D−1 excess) for next-day fade filter.
- Sector indices (other `IX*` 5m): **none**.
- Note: IX0001 5m is OOS-only vs IS≤2025-09-30; intraday RS uses 0050 proxy; overnight RS uses IX0001 daily.

## Metric / freeze

- **Directed hit (~30m)**: `temp∈{±1}` and `|fwd|≥0.0005`; hit iff `sign(temp)==sign(fwd)`.
- Forward: `close[i+6]/close[i]-1` @ 5m.
- PIT: stock+bench factors use completed same-session bars `≤ i`; overnight excess uses **D−1** only.
- IS / OOS: IS `≤ 2025-09-30`; champion on **IS only**; claim on OOS.
- Universe (ex `0050`): `2327, 8046, 3189, 6451, 2330, 2303, 2454`.
- Pre-registered variants: 13.
- Window: **2024-01-02 → 2026-07-22**

## Verdict: OOS≥70% + stable（0050 proxy）？ **YES**

- IS champion: `fade_idx_or_inside`
- Champion IS hit: **71.57%** (n=1333)
- Champion OOS hit (0050 bench): **70.66%** (n=634)
- Best OOS among grid (exploratory): `fade_idx_or_inside` → **70.66%** (n=634)
- Helps fade30 toward 70%? **PARTIAL — 0050-proxy gates pass (OOS 70.66%), but IX0001 OOS sensitivity 66.86% <70%; not robust to true TAIEX yet**

## Gates（IS champion OOS · 0050 proxy）

- `gate_oos_hit_ge70_n500`: **True**
- `gate_name_stability`: **True** (6/7 = 0.857)
- `gate_no_megacap_dom`: **True** (max share 0.243 · 2330)

## OOS sensitivity · true TAIEX `IX0001` 5m（no IS）

- Champion `fade_idx_or_inside`: **66.86%** (n=851)
- Unfiltered fade: **65.36%** (n=1536)
- Claim OOS≥70% on true index: **NO**

## Pool leaderboard（OOS directed 排序 · 0050 bench）

|variant|family|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|Δ vs fade|
|---|---|---:|---:|---:|---:|---:|---:|
|fade_idx_or_inside|fusion|71.57|1333|70.66|634|20.66|6.64|
|fade_rs30_against|fusion|68.71|2659|65.22|1452|15.22|1.2|
|baseline_fade_near_ext|baseline|68.56|3158|64.02|1651|14.02|0.0|
|fade_ovn_rs_same|fusion|67.36|1486|63.23|718|13.23|-0.79|
|fade_ovn_rs_opp|fusion|69.65|1420|63.12|873|13.12|-0.9|
|fade_ovn_rs_strong_same|fusion|67.07|1145|60.85|613|10.85|-3.17|
|baseline_vs_idx_orb|baseline|47.94|20499|49.77|16245|-0.23|-14.25|
|baseline_rs_day|baseline|47.94|49671|49.36|41573|-0.64|-14.66|
|baseline_rs60|baseline|44.26|37267|46.97|33378|-3.03|-17.05|
|baseline_vs_idx_vwap|baseline|45.98|45718|46.22|39353|-3.78|-17.8|
|baseline_rs30|baseline|42.25|39978|44.71|36610|-5.29|-19.31|
|and_rs30_vs_idx_vwap|fusion|42.17|26366|43.61|23742|-6.39|-20.41|
|fade_rs30_agree|fusion|68.75|96|32.61|46|-17.39|-31.41|

## IS champion per-stock OOS · `fade_idx_or_inside`

|sid|OOS hit%|OOS n|E[signed]%|
|---|---:|---:|---:|
|2330|88.96|154|0.3255|
|2454|75.57|131|0.2544|
|2327|73.24|71|0.2084|
|8046|72.41|29|0.5192|
|2303|56.45|62|0.084|
|3189|55.71|70|-0.265|
|6451|55.56|117|0.0464|

## Interpretation

Standalone intraday RS (excess 30m/60m/day), vs-index-VWAP, and vs-index-ORB are ~coin-flip or worse — same failure mode as raw mom. Overnight IX0001 RS filters on fade30 mostly thin the sample without clearing 70%. The IS-locked champion under the **0050** proxy protocol is **fade_idx_or_inside**: fade_near_ext only when the proxy opening range is still intact. That lifts unfiltered fade OOS ~64% → ~70.7% (n=634). OOS sensitivity with true TAIEX IX0001 5m: champ 66.86% (n=851) — **below 70%**, so the gate pass is proxy-dependent. Caveats: IX0001 5m has 0 IS days; n barely ≥500 on 0050; 2330 still strong; soft names near 55% floor. Do not rewrite Live ta_30m_bias without a fresh holdout / true-index confirmation.

## Artifacts

- JSON: `reports/research/intraday_direction_thermometer/vs_market_30m_backtest.json`
- MD: `reports/research/intraday_direction_thermometer/TRACK_VS_MARKET.md`
- Runner: `scripts/research/run_vs_market_30m_backtest.py`

