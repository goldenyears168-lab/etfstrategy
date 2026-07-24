# Track · 價量 (price–volume) · ~30m directed bias

Research only · **未採納** · not Order / not `strategy.yaml`

## Data path

- Research PIT: `stock_kbar_5m.volume` in `data/stocks.db` （universe 量能覆蓋 ≈98–100%；窗 2024-01-02→2026-07-22）。
- Live observe mirror: Yahoo Chart 5m OHLCV via `ops_live_ta.fetch_yahoo_5m_bars` / `compute_ta_30m_snapshot` （本 track 不打 Yahoo）。

## Metric（共用協議）

- **Directed hit (~30m)**: `temp ∈ {±1}` 且 `|fwd| ≥ 0.0005`；hit iff `sign(temp)==sign(fwd)`。
- **Forward**: `close[i+6]/close[i]-1`（≈30 分 @ 5m）。
- **PIT**: 僅用當日已完成 bars `≤ i`。
- **IS / OOS**: IS ≤ `2025-09-30`；OOS `>`。選冠軍只看 IS；**主閘只認一次 OOS**。
- **對照**: `fade_near_ext` ~62%；`mom30` ~45%；目標 OOS≥70% + 穩定。
- Universe: `2327, 8046, 3189, 6451, 2330, 2303, 2454, 0050`
- Pre-registered variants: 14

## Verdict: OOS≥70% + stable？ **NO**

- IS champion: `fade_ud_agree`
- Champion IS: **74.78%** (n=801)
- Champion OOS: **59.81%** (n=520)
- Best OOS in grid (exploratory): `fade_dryup_05` → **68.09%** (n=796)

## Does volume lift fade to 70%?

- Fade baseline OOS: **62.13%** (n=1846)
- Best fade×volume OOS: `fade_dryup_05` → **68.09%** (n=796)
- Δ vs fade baseline: **5.96 pp**
- Reaches 70%? **NO**

價量濾網相對裸 fade 改變 +5.96 pp；未達 70%（或 n 不足）；dry-up／無放量多為縮樣，未穩定抬升命中。

## Gates（IS champion OOS）

- `gate_oos_hit_ge70_n500`: **False**
- `gate_name_stability`: **False** (3/8 = 0.375)
- `gate_no_megacap_dom`: **True** (max share 0.227 · 2303)

## Pool leaderboard（OOS directed 排序）

|variant|family|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|
|---|---|---:|---:|---:|---:|---:|
|fade_dryup_05|fade_vol|73.9|1475|68.09|796|18.09|
|fade_dryup_07|fade_vol|72.37|1889|66.28|958|16.28|
|fade_dryup07_ud_against|fade_vol|69.91|1253|65.13|628|15.13|
|fade_no_surge_1p2|fade_vol|69.66|2502|63.44|1269|13.44|
|fade_ud_against|fade_vol|64.51|2595|62.99|1305|12.99|
|baseline_fade_near_ext|baseline|66.86|3467|62.13|1846|12.13|
|fade_no_surge_1p5|fade_vol|68.38|2701|61.87|1390|11.87|
|fade_ud_agree|fade_vol|74.78|801|59.81|520|9.81|
|updown_vol_L6|vol|48.2|68232|50.59|52338|0.59|
|or_break_vol_surge_1p5|vol|49.23|2781|46.85|2352|-3.15|
|baseline_mom30|baseline|43.79|40930|45.17|38807|-4.83|
|or_break_vol_surge_2p0|vol|48.09|1574|42.92|1491|-7.08|
|vol_surge_mom_1p5|vol|44.96|3801|41.34|3689|-8.66|
|vol_surge_mom_2p0|vol|45.54|2141|39.13|2502|-10.87|

## IS champion per-stock OOS · `fade_ud_agree`

|sid|OOS hit%|OOS n|E[signed]%|
|---|---:|---:|---:|
|2327|86.21|29|0.4822|
|2330|75.82|91|0.2023|
|8046|72.22|18|0.6541|
|2454|70.0|100|0.2533|
|0050|60.0|10|0.04|
|6451|55.21|96|0.2368|
|3189|51.72|58|0.1091|
|2303|38.14|118|-0.2585|

## Sensitivity · flat_eps=0.10%

- Same IS champion OOS @ flat 0.10%: **59.65%** (n=518)

## Honest note

IS 冠軍 `fade_ud_agree` OOS=59.81% (n=520)；格子最佳 OOS `fade_dryup_05`=68.09% (n=796)。 裸 fade OOS=62.13%；最佳 fade×volume `fade_dryup_05`=68.09% (Δ+5.96pp)。 放量追動能／OR+surge 仍貼近或低於擲幣；量能未能把 fade 抬到 70%。

## Do not

- 未過閘寫入 `strategy.yaml` / Order live
- 事後擴格子再挑 OOS 冠軍當宣稱

Generated: `2026-07-24T10:57:00`
Runner: `scripts/research/run_ta_30m_track_price_volume.py`
