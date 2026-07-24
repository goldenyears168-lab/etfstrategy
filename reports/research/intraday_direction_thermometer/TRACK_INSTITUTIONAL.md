# Track · 三大法人 × TA 30m bias

Research only · **未採納** · not Order / not `strategy.yaml`

## Data sources

- Table: `stock_institutional_daily` (source=`finmind`)
- Sync: `src/sync_stock_market_daily.py`
- Columns: `foreign_net`, `investment_trust_net`, `dealer_self_net`, `three_institution_net`
- Coverage (universe): 2327:605/605d, 8046:215/215d, 3189:144/144d, 6451:184/205d, 2330:613/613d, 2303:611/611d, 2454:611/611d, 0050:277/611d

## Data lag（PIT · critical）

三大法人為**收盤後**公布（FinMind 灌庫）。日內 10:00–12:30 決策僅能合法使用 **T−1（及更早）** 淨買賣；同日 T 數字要等到當日收盤後才可用於隔日。

| Clock on day T | Available institutional | Used in this track? |
|---|---|---|
| **10:00 / midday fade window** | prints with `trade_date ≤ T−1` | **Yes** |
| After TWSE EOD publish (~16:00+) | same-day `T` nets | **No** (not for 30m same-day) |

## Metric（shared protocol）

- **Directed hit (~30m)**: `temp ∈ {±1}` and `|fwd| ≥ 0.0005` (= 0.05%), hit iff `sign(temp) == sign(fwd)`.
- **Forward**: `close[i+6] / close[i] - 1`（5m）。
- **Price PIT**: completed 5m bars `≤ i` only.
- **Chip PIT**: institutional features as-of **T−1** only.
- **IS / OOS**: IS `≤ 2025-09-30`；OOS `>`。Select on IS；gates on OOS.
- **Gates**: OOS≥70% n≥500；≥70% names OOS≥55% n≥50；max name ≤40% n.

- Window: **2024-01-02 → 2026-07-22**
- Universe: `2327, 8046, 3189, 6451, 2330, 2303, 2454, 0050`
- Pre-registered variants: 13

## Verdict: helps fade30 toward OOS≥70%？ **NO**

- IS champion: `fade_foreign_streak3`
- Champion IS hit: **76.43%** (n=543)
- Champion OOS hit: **54.84%** (n=217)
- Best OOS in grid (exploratory): `fade_trust_buy_t1` → **66.63%** (n=830)
- Unfiltered fade OOS: **62.13%** (n=1846)
- Δ vs unfiltered fade (champion OOS): **-7.29** pp

## Gates（IS champion OOS）

- `gate_oos_hit_ge70_n500`: **False**
- `gate_name_stability`: **False** (0/8 = 0.0)
- `gate_no_megacap_dom`: **False** (max share 0.456 · 2303)

## Features（T−1 as-of）

| feature | definition |
|---|---|
| `foreign_net` | 外資買賣超（股）T−1 |
| `trust_net` | 投信買賣超 T−1 |
| `dealer_net` | 自營（自行買賣）T−1 |
| `three_net` | 三大合計 T−1 |
| `*_net_5d` | 近 5 個法人交易日累計（含 T−1） |
| `foreign_buy_streak` | 外資連續淨買日數（止於 T−1） |
| `three_buy_streak` | 三大合計連續淨買日數 |

## Pool leaderboard（OOS directed 排序）

|variant|family|IS hit%|IS n|OOS hit%|OOS n|OOS vs coin|
|---|---|---:|---:|---:|---:|---:|
|fade_trust_buy_t1|filter|67.48|1636|66.63|830|16.63|
|fade_foreign_sell_t1|filter|67.02|1692|66.47|1035|16.47|
|fade_against_foreign|filter|70.23|1562|64.75|871|14.75|
|fade_three_sell_t1|filter|68.06|1650|64.01|1081|14.01|
|fade_foreign_buy_trust_nn|filter|67.8|736|63.41|410|13.41|
|baseline_fade_near_ext|baseline|66.86|3467|62.13|1846|12.13|
|fade_three_5d_buy|filter|71.39|1650|59.91|696|9.91|
|fade_align_foreign|filter|67.43|1624|59.79|975|9.79|
|fade_three_buy_t1|filter|69.6|1536|59.48|765|9.48|
|fade_foreign_buy_t1|filter|70.82|1494|56.6|811|6.6|
|fade_foreign_streak3|filter|76.43|543|54.84|217|4.84|
|fade_foreign_streak2|filter|74.66|809|53.71|404|3.71|
|null_foreign_dir_midday|null|50.01|42355|49.07|37102|-0.93|

## IS champion per-stock OOS · `fade_foreign_streak3`

|sid|OOS hit%|OOS n|E[signed]%|
|---|---:|---:|---:|
|2330|80.95|21|0.1618|
|2454|75.0|16|0.1073|
|2327|66.67|3|-0.1329|
|8046|66.67|12|0.2182|
|0050|62.5|8|0.0215|
|6451|56.0|25|0.4206|
|2303|48.48|99|0.0864|
|3189|39.39|33|-0.2677|

## Honest read

IS 選冠軍 `fade_foreign_streak3` **嚴重過擬合**（IS 76.43% → OOS 54.84%，n=217＜500），相對未過濾 fade **−7.29 pp**。
格子內探索最佳 OOS 為 `fade_trust_buy_t1` **66.63%**（n=830）與 `fade_foreign_sell_t1` **66.47%**（n=1035）——相對 fade 基準約 **+4.5 pp**，仍短於 70% 閘，且**不得**事後改選為宣稱冠軍。
外資「連買 streak」抬 IS、傷 OOS；投信買超／外資賣超過濾較穩但仍未達標。
法人為日終資料，盤中 30m 只能用 T−1；**不幫助穩定達到 OOS≥70%**。

## Sensitivity · flat_eps=0.10%

- Same IS champion OOS @ flat 0.10%: **54.42%** (n=215)

## Next steps

- 若需盤中即時法人流，改用分點/券商支流或盤中估計，非 TWSE 日終三大法人。
- 可試「fade × 外資連賣」僅短邊／「連買」僅長邊的非對稱規則（仍須一次 IS 鎖定）。
- 與 branch-specialist / whale chip 合流時，法人只當 regime 慢變數，勿當 30m alpha。
- 0050/6451 法人覆蓋較短；擴宇宙前先補齊 `stock_institutional_daily`。

## Do not

- 未過閘寫入 `strategy.yaml` / Order live
- 用同日盤中尚未公布的法人數字當 10:00 特徵（lookahead）
- 事後擴格挑 OOS 當宣稱

Generated: `2026-07-24T10:57:42`
Script: `scripts/research/run_track_institutional_ta30m.py`
