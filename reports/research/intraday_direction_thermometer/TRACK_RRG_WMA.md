# TRACK · RRG + WMA3 + WMA5（regime filter for fade30）

Research only · **未採納** · not Order / not `strategy.yaml`

## Horizon honesty（先講清楚）

- Repo **RRG / dual-WMA**（`WMA_MICRO=3` · `WMA_SHORT=5` · `WMA_LONG=20`）是 **日線** JdK RRG legs（`rrg_rotation.compute_rrg_panel`），不是 30m TA 預測器。
- 本 track 另算 **價格** 日線 WMA3 / WMA5（同 `rrg_rotation.wma`）與交叉。
- **Native horizon** = 日線收盤訊號 → 次日（+1d；附 +5d）directed hit。
- **Intraday 用法** = 僅當 `fade_near_ext`（≈30m）的 **regime filter**； lag = **前一交易日** EOD（盤中 PIT）。
- 若日線 filter 無法把 fade30 OOS 推到 ≥70%：**誠實說不行**（見 Verdict）。

## Code locations

- `src/rrg_rotation.py` — JdK RRG · wma() · compute_rrg_panel · classify_quadrant · default L=20 daily
- `src/rrg_universe_intraday_panel.py` — WMA_MICRO=3 · WMA_SHORT=5 · WMA_LONG=20 · dual/triple RRG legs（日線+可選盤中覆寫 close）
- `src/research/backtest/dual_wma_signal_backtest.py` — Dual-WMA RRG signal events（日線腿 · 非 price WMA cross）
- `src/research/backtest/rrg_mono_score_swap_c.py` — rrg-c18acc / score-swap-C · uses W5/W20 RRG panels
- `src/research/intraday_direction_thermometer.py` — fade_near_ext_from_bars · FADE_HORIZON_BARS=6 ≈30m

- Window: **2024-01-02 → 2026-07-22** · IS ≤ `2025-09-30`
- Universe: `2327, 8046, 3189, 6451, 2330, 2303, 2454, 0050`
- Fade horizon: H=6 ≈30m @ 5m · flat_eps=0.0005

## Verdict: OOS≥70% stable ~30m？ **NO**

- IS champion filter: `fade_price_vs_wma5_align`
- Champion IS hit: **70.22%** (n=1578)
- Champion OOS hit: **60.77%** (n=938)
- Baseline `fade_only` OOS: **62.13%** (n=1846) · vs known ~62%
- Naive daily RRG→30m OOS: **48.97%** (n=58768) · 不應當 30m predictor

### Gates（IS champion OOS）

- `gate_oos_hit_ge70_n500`: **False**
- `gate_name_stability`: **False** (4/8 = 0.5)
- `gate_no_megacap_dom`: **True** (max share 0.192 · 2330)

## (a) Native daily horizon · directed hit

Forward = close[t+H]/close[t]-1 · temp from EOD features on t（PIT closes ≤ t）。

### Horizon +1d

|rule|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|
|---|---:|---:|---:|---:|---:|
|rrg20_quad|49.19|2407|51.16|1466|1.16|
|price_vs_wma5|48.06|2499|51.03|1454|1.03|
|price_vs_wma3|48.15|2507|50.75|1460|0.75|
|combo_price_wma_stack|48.42|2117|50.66|1218|0.66|
|rrg3_mom|49.86|2503|50.14|1466|0.14|
|wma3_gt_wma5|49.26|2499|50.07|1454|0.07|
|combo_rrg5_wma_cross|48.53|1803|48.2|1025|-1.8|
|rrg5_quad|48.64|2492|47.27|1466|-2.73|
|rrg5_mom|48.64|2492|47.27|1466|-2.73|

### Horizon +5d

|rule|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|
|---|---:|---:|---:|---:|---:|
|combo_price_wma_stack|46.15|2210|51.91|1233|1.91|
|price_vs_wma5|47.21|2620|51.77|1472|1.77|
|wma3_gt_wma5|46.3|2620|51.43|1472|1.43|
|price_vs_wma3|48.63|2628|51.29|1472|1.29|
|rrg20_quad|49.98|2529|51.15|1478|1.15|
|combo_rrg5_wma_cross|45.93|1894|50.1|1040|0.1|
|rrg3_mom|48.06|2624|49.26|1478|-0.74|
|rrg5_quad|47.75|2618|48.85|1478|-1.15|
|rrg5_mom|47.75|2618|48.85|1478|-1.15|

## (b) Filter on `fade_near_ext` · fwd ≈30m

Daily features lagged to **prior session** · fade midday window unchanged。

|variant|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|ΔOOS vs fade_only|
|---|---:|---:|---:|---:|---:|---:|
|fade_price_vs_wma5_contra|67.46|1589|64.79|869|14.79|2.66|
|fade_rrg5_wma_combo_contra|67.87|1111|64.57|604|14.57|2.44|
|fade_rrg5_quad_mr|68.63|1594|64.02|845|14.02|1.89|
|fade_rrg5_mom_contra|68.63|1594|64.02|845|14.02|1.89|
|fade_wma3_gt_wma5_align|69.92|1579|62.73|915|12.73|0.6|
|fade_wma3_gt_wma5_contra|67.76|1588|62.67|892|12.67|0.54|
|fade_rrg5_wma_combo_align|70.04|1108|62.46|682|12.46|0.33|
|fade_only|66.86|3467|62.13|1846|12.13|0.0|
|fade_rrg5_quad_risk_on|69.1|1589|61.29|979|11.29|-0.84|
|fade_rrg5_mom_align|69.1|1589|61.29|979|11.29|-0.84|
|fade_price_vs_wma5_align|70.22|1578|60.77|938|10.77|-1.36|
|naive_rrg5_mom_as_30m|50.08|66292|48.97|58768|-1.03|-13.16|

### IS champion per-stock OOS · `fade_price_vs_wma5_align`

|sid|OOS hit%|OOS n|E[signed]%|
|---|---:|---:|---:|
|2327|79.49|78|0.4112|
|2330|78.33|180|0.2097|
|2454|67.05|173|0.1799|
|8046|66.67|30|0.3897|
|6451|58.4|125|0.4281|
|0050|48.94|94|-0.0862|
|3189|45.67|127|-0.213|
|2303|41.22|131|-0.0974|

## Honest takeaway

- Daily RRG/WMA **alone** are mediocre next-day predictors（OOS typically ~50–55% range；見表）。
- Using them as **30m predictors**（naive control）is near coin-flip — do not confuse with sleeve RRG。
- As **filters** on fade≈62% baseline：best IS-selected filter still **well below 70% OOS**； some filters raise hit slightly但常犧牲 n，且穩定性不足。
- **Cannot claim OOS≥70%** from this track。

- Script: `scripts/research/run_rrg_wma_fade_filter_study.py`
- JSON: `reports/research/intraday_direction_thermometer/track_rrg_wma_backtest.json`

