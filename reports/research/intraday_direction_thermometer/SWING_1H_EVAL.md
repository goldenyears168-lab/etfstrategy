# Swing 1h thermometer · PIT directed-hit evaluation

Research only · **未採納** · not Order / not Live trading signal

## Metric definition（預先鎖定）

### Primary（本輪閘門）

- **Directed hit rate (~60m)**: samples with `temp ≠ 0` and `|fwd| ≥ 0.05%`, hit iff `sign(temp) == sign(fwd)`.
- **Forward**: `close[i+12] / close[i] - 1`（≈60 分；同日 5m K）。
- **Signal family**: frozen `swing_1h_from_bars` + minimal knobs (L / ready / mag / slope-only / open_guard / sparse / MR flip).
- **PIT**: 訊號僅用當日已完成 5m bars `≤ i`。
- **Null**: coin-flip 50%；always-long = directed 中 fwd>0 比例。
- **IS / OOS**: IS `trade_date ≤ 2025-09-30`；OOS `> 2025-09-30`。 調參只看 IS；**主閘門宣稱 ≥60% 只認 Primary OOS**。
- **Gate**: OOS directed ≥ **60%** 且 pool `n_directed ≥ 500`。

### Secondary（旁支 · 不取代主閘）

- `fade_near_ext_from_bars`：午盤贴近日高/日低之 **~30m** 均值回歸淡化。
- 即使 secondary OOS ≥60%，**不得**宣稱 Primary Swing 1h 過閘。

- Window: **2024-01-02 → 2026-07-22**
- Universe: `2327, 8046, 3189, 6451, 2330, 2303, 2454, 0050`（2492 無 kbar_5m → 排除）

## Verdict: Primary OOS ≥60%？ **NO**

## Secondary fade30m OOS ≥60%？ **YES** （旁支；n=1846 hit=62.13%）

- Best Primary OOS variant: `mr_mag030_L12`
- Primary OOS directed: **51.85%** (n=27122)
- Primary OOS always-long null: 47.09%
- Primary OOS edge vs coin: 1.85 pp
- Primary IS directed (same variant): 53.03% (n=28469)

## Pool leaderboard · Primary ~60m（OOS directed 排序）

|variant|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|OOS vs long|OOS E[signed]%|
|---|---:|---:|---:|---:|---:|---:|---:|
|mr_mag030_L12|53.03|28469|51.85|27122|1.85|4.76|0.0047|
|mr_slope_L12|51.92|45290|51.58|37454|1.58|3.72|0.0037|
|slope_only_L12|48.08|45290|48.42|37454|-1.58|0.56|-0.0037|
|best_candidate|46.19|26059|47.93|26352|-2.07|0.85|-0.0003|
|slope_only_L8|47.25|42871|47.92|36929|-2.08|0.14|0.0002|
|mag030_slope_only|45.33|23341|47.69|24669|-2.31|0.78|0.0042|
|mag050_slope_only|45.91|10208|47.6|16568|-2.4|-0.01|0.0083|
|sparse_mag030|45.85|2447|47.5|2804|-2.5|0.89|0.0365|
|mag050_L8|44.91|9043|46.93|13743|-3.07|-0.81|-0.0058|
|L12_R12|46.14|41178|46.84|32130|-3.16|-0.97|-0.0228|
|mag030_L8|44.09|20694|46.67|20356|-3.33|-0.45|-0.0128|
|after_guard_mag030|44.09|20694|46.67|20356|-3.33|-0.45|-0.0128|
|L10_R12|45.93|41385|46.61|32520|-3.39|-1.46|-0.0211|
|mag015_L8|45.33|29168|46.61|26446|-3.39|-0.78|-0.0172|
|baseline_L8_R12|45.58|41486|46.47|33016|-3.53|-1.64|-0.0196|
|after_guard|45.58|41486|46.47|33016|-3.53|-1.64|-0.0196|
|sparse_101112|46.75|4244|46.38|3523|-3.62|-2.56|0.0011|
|L8_R8|45.78|47501|46.37|36333|-3.63|-1.7|-0.0243|
|strong_only|44.97|19246|46.22|18491|-3.78|-2.58|-0.0201|
|L6_R12|45.29|41689|46.19|33599|-3.81|-2.08|-0.0216|
|strong_mag030|43.01|11348|45.99|13417|-4.01|-2.17|-0.0243|

## Secondary · fade_near_ext ~30m pool

|split|hit%|n|always-long%|E[signed]%|
|---|---:|---:|---:|---:|
|IS|66.86|3467|52.12|0.1708|
|OOS|62.13|1846|50.87|0.1175|

### fade30m per stock (OOS)

|sid|OOS hit%|OOS n|E[signed]%|
|---|---:|---:|---:|
|2327|74.07|162|0.3179|
|8046|67.9|81|0.3533|
|3189|47.98|173|-0.1461|
|6451|55.31|273|0.1213|
|2330|78.91|384|0.2275|
|2303|44.64|233|-0.1216|
|2454|69.86|345|0.2452|
|0050|46.15|195|-0.0756|

## Baseline (`baseline_L8_R12`) per stock · Primary 60m

|sid|days|IS hit%|IS n|OOS hit%|OOS n|OOS always-long%|
|---|---:|---:|---:|---:|---:|---:|
|2327|605|45.42|8062|43.75|4889|49.21|
|8046|215|47.14|1589|47.37|3654|47.13|
|3189|144|45.47|1612|50.77|2025|48.35|
|6451|205|53.33|510|48.48|2923|38.97|
|2330|613|41.61|7020|38.62|3967|51.12|
|2303|611|46.9|7925|48.67|5093|45.36|
|2454|611|41.01|6867|43.25|4536|46.74|
|0050|611|51.13|7901|51.54|5929|53.65|

## What worked / failed

- **Failed (Primary 60m)**: frozen momentum+short fusion、L/ready 掃描、magnitude gate、sparse clock — OOS directed 大致 45–49%，弱於或贴近硬币。
- **Partial**: 把斜率改成均值回歸（`mr_*`）可把 Primary OOS 抬到 ~51–53%，仍 <60%。
- **Secondary worked (不同地平線)**: 午盤贴近日極值之 ~30m fade，OOS directed **62.13%** (n=1846)；但個股異質（部分標的 <50%），且 **不是** Swing 1h/~60m 主指標。
- **Hard stop（Primary）**: 全網格 Primary OOS <55% 且 n≥min_oos_n → 停止再掃 L；下一輪需新資訊源（大盤5m／量能結構／隔夜gap），或獨立開題驗證 fade30m（fresh holdout／跨宇宙）。

## Robustness notes · fade30m（旁支）

Frozen half-year folds（參數固定為 IS-champion，未每折重訓）:

|test window|hit%|n|E[signed]%|
|---|---:|---:|---:|
|2024-07→2024-12|67.46|1094|0.179|
|2025-01→2025-06|65.15|1053|0.151|
|2025-07→2025-12|69.53|1411|0.166|
|2026-01→2026-07|60.09|1095|0.128|

Leave-one-stock-out OOS：剔除 **2330** 後 pool 掉到 **57.7%**（<60%）→ 邊主要由 2330/2454/2327 貢獻；0050/2303/3189 OOS <50%。  
**結論**：secondary 過閘但**不穩**，不可當通用 Live overlay。

## Recommendation

- Primary Swing 1h (~60m) gate **未過** → **留在 research**；不建議把現有 1h 溫度當交易 overlay。
- Secondary fade30m OOS ≥60% → 僅可作 **研究候選／observe 標註**；需先過個股穩定性與 fresh holdout，再談 Live overlay。
- **不採納**進 `config/strategy.yaml` / Order。

- JSON: `reports/research/intraday_direction_thermometer/swing_1h_backtest.json`
- Runner: `scripts/research/run_swing_1h_thermometer_backtest.py`
- Helper: `fade_near_ext_from_bars` in `src/research/intraday_direction_thermometer.py`

