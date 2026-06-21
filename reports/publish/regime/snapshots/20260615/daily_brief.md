# Market structure memo · 2026-06-15

> Weinstein · Minervini · Kempenaer RRG · Zweig / Deemer breadth


> 專案層：**Regime four-axis diagnostic（四軸體制診斷）** · 本 memo 整合下列作者方法之每日快照；非 live gate、非 alpha、非策略績效。


> 本 memo 依四條作者／業界標準路徑描述台股加權指數與研究樣本：
>
> 1. **% above MA**（Deemer 系統計）— 50 日／200 日均線上方股票占比
> 2. **Weinstein Stage Analysis** — 週線 Stage 1–4 與 30-week MA
> 3. **Relative Rotation Graphs**（Kempenaer RRG）— RS-Ratio × RS-Momentum 四象限
> 4. **Minervini Trend Template** — universe 內個股八項模板通過率
>
> 建議：先看 **Daily synopsis** → 逐章讀 **Notes** → 圖表請開 [`daily_brief.html`](daily_brief.html)（Markdown 預覽不顯示 SVG）。

> **含圖請開** [`daily_brief.html`](daily_brief.html)（瀏覽器）· Cursor Markdown 預覽**不顯示**本地 SVG。

## Daily synopsis（每日摘要）


> **Daily synopsis** 將四路徑各一句合成摘要。若 % above 200-day MA 偏高但 Minervini pass rate 偏低，常見於指數由少數大型股拉抬。

IX0001 · Weinstein Stage 2 · advancing（上升）；% above 200-day MA 91.7%（Overbought · 過熱 (>80%)）；Zweig EMA rhythm 55.4%（Mid · 中等 (50–58%)）；RRG Leading + Improving 47.1%；Minervini template pass rate 50.0%。 200-day 廣度偏高但 Minervini pass rate 仍高，漲幅擴散較廣。 綜合：200MA Level 偏高、Zweig EMA rhythm 中等偏強，但 Thrust 窗口未 active → 高位慣性，非剛點火。

---

## 1 · Breadth axis · Level / Rhythm / Impulse


> **Breadth axis** 分三層（皆為 Regime 診斷 · 非 Strategy overlay）：
>
> **1A Level · % above MA**（Deemer 系統計）：50-day / 200-day MA 上方占比。
>
> **1B Rhythm · Zweig EMA rhythm tier**：adv/decl ratio 的 10-day EMA 分 tier（off / low / mid / high）。描述市場參與**節奏**，有別於 200MA **水位**。
>
> **1C Impulse · Zweig Breadth Thrust / Deemer BAM**：事件層 thrust 與 breakaway momentum。
>
> **50 vs 200 spread：** 50-day 廣度減 200-day 廣度。**Advance/decline divergence：** 指數近 20 日向上而 50-day 廣度走弱。


### 1A · Breadth level · % Above MA

**Overbought · 過熱 (>80%)**


> **Level** 讀 200-day MA 五區間（oversold → overbought）。圖表上：加權指數 + 50MA／200MA 廣度 %；背景色為 200-day 分區間。

**Notes**

**% above 200-day MA 91.7%**（Overbought · 過熱 (>80%)）；**% above 50-day MA 79.0%**。50 vs 200 spread **-12.7 pp**，50-day 低於 200-day。 50-day MA 近 5 日上升 3.0pp；200-day MA 近 5 日上升 0.8pp。 未見指數漲／50-day 廣度降之背離。 200-day 廣度處高位區間；屬環境描述，請搭配 Weinstein Stage 與 RRG 閱讀。

| Reading | Value |
|---------|-------|
| % above 200-day MA | 91.7% |
| % above 50-day MA | 79.0% |
| 5d Δ (50 / 200) | +3.0pp / +0.8pp |
| 50 vs 200 spread | -12.7pp |
| Advance/decline divergence | no |
| Universe n | 133 |

![% Above MA · index + breadth panel](axis/breadth/spark.svg)


### 1B · Zweig EMA rhythm tier


> **Zweig EMA rhythm tier**（Zweig / Deemer 廣度傳統）：adv/decl 日線 ratio 的 10-day EMA，依 tier 閾值分 off / low / mid / high。Research validation 顯示 rhythm tier 具統計增量；Regime 僅報讀 tier，**不含 exposure 仓位**。

**Mid · 中等 (50–58%)**

**Notes**

**Zweig EMA rhythm tier** · adv/decl 10-day EMA **55.4%**（Mid · 中等 (50–58%)）。 5d Δ 近 5 日上升 13.9pp。 Rhythm 中等，市場參與節奏尚可。

| Reading | Value |
|---------|-------|
| Zweig adv/decl 10-day EMA | 55.4% |
| Rhythm tier | mid |
| 5d Δ | +13.9pp |

![Zweig EMA rhythm · 90d](axis/breadth/zweig_ema_spark.svg)


### 1C · Breadth impulse · Zweig thrust / Deemer BAM


> **Impulse** 偵測 thrust 事件：Zweig 以 adv/decl EMA 穿越偵測 Breadth Thrust；Deemer 以 10-day adv/decl ratio 偵測 BAM。Thrust 窗口 active 表示近期曾觸發 thrust／BAM，仍在 hold 期內。

**Notes**

Thrust 窗口未 active

| Reading | Value |
|---------|-------|
| Deemer 10-day adv/decl | 0.96 |
| Zweig thrust today | no |
| Deemer BAM today | no |
| Thrust window active | no |
| Days remaining | 0 / 42 |

**Combined read**

綜合：200MA Level 偏高、Zweig EMA rhythm 中等偏強，但 Thrust 窗口未 active → 高位慣性，非剛點火。

## 2 · Weinstein Stage Analysis · weekly


> **Weinstein Stage Analysis（1988）** 以 **週線** 加權指數判 Stage：1 basing → 2 advancing → 3 topping → 4 declining；基準為 **30-week MA**。圖底 **Stage ribbon** 為週線 Stage 著色（紫 S1、綠 S2、橙 S3、紅 S4）。
>
> **Minervini Trend Template（2013）** 八條日線規則檢驗指數是否處 Stage 2 型上升結構。

### IX0001 · **Stage 2 · advancing（上升）**

**Notes**

**IX0001** 週線 **Stage 2 · advancing（上升）**：收盤 在 **30-week MA** 上，MA 斜率 +7.03%，偏離 30-week MA **+30.6%**，higher lows 成立。 偏離 MA 較大，屬 Stage 2 後段（Weinstein topping 觀察區，非賣出訊號）。 **Minervini Trend Template**（指數）**7/8** passed；Stage 2 型結構仍完整。

| Reading | Value |
|---------|-------|
| 30-week MA slope | 7.03% |
| Extension vs 30-week MA | 30.56% |
| Higher lows | yes |
| Price above 30-week MA | yes |

![IX0001 weekly · Weinstein Stage ribbon](axis/trend/weinstein_weekly.svg)


### Minervini Trend Template · IX0001


| Criterion | 說明 | Pass |
|-----------|------|------|
| Price > SMA150 & SMA200 | 收盤價高於 150 日與 200 日均線 | ✓ |
| SMA150 > SMA200 | 150 日均線在 200 日之上 | ✓ |
| SMA200 trending up (22d) | 200 日均線近 22 日向上 | ✓ |
| SMA50 > SMA150 & SMA200 | 50 日均線高於 150 與 200 日 | ✓ |
| Price > SMA50 | 收盤價高於 50 日均線 | ✓ |
| ≥30% above 52w low | 距 52 週低點至少漲 30% | ✓ |
| Within 25% of 52w high | 距 52 週高點不超過 25% | ✓ |
| RS rank > 70 | RS 排名 > 70（指數端略過） | ✗ |

*Summary: 7/8 passed*

## 3 · Relative Rotation Graphs · Kempenaer RRG


> **Relative Rotation Graphs**（Julius de Kempenaer · StockCharts 實作）：個股相對 benchmark 畫在 RS-Ratio（JdK）× RS-Momentum 平面。
>
> - **Leading**：相對強、動量強
> - **Improving**：相對弱、動量轉強
> - **Weakening**：相對強、動量轉弱
> - **Lagging**：相對弱、動量弱
>
> **Symbol table** 依象限排序（StockCharts 慣例）；**tail** 為近 4 交易日軌跡。**Quadrant migration** 為 1 日跨象限檔數。

**Leading + Improving: 47.1%** · n=157

**Notes**

**Leading + Improving** 占樣本 **47.1%**（Leading 22.3% · Improving 24.8%）。Weakening 17.2% · Lagging 35.7%。 四象限分散，宜看 migration 與 symbol table，不宜只看最大象限。 1-day migration：Improving→Leading **0** · Leading→Weakening **1** · Lagging→Improving **0** · Weakening→Lagging **0**。

| Quadrant | Count | Share |
|----------|------:|------:|
| Leading | 35 | 22.3% |
| Improving | 39 | 24.8% |
| Weakening | 27 | 17.2% |
| Lagging | 56 | 35.7% |

| 1-day quadrant migration | Count |
|--------------------------|------:|
| Improving → Leading | 0 |
| Leading → Weakening | 1 |
| Lagging → Improving | 0 |
| Weakening → Lagging | 0 |

### RRG symbol table（Kempenaer · StockCharts）

依象限排序；RS-Ratio（JdK）· RS-Momentum · 4-day tail。

| Quadrant | Symbol | RS-Ratio | RS-Mom | Tail |
|----------|--------|----------|--------|------|
| Leading | 2059 | 109.5 | 105.31 | ↗ up-right（相對走強） |
| Leading | 2883 | 107.53 | 105.13 | → up-left（動量轉弱） |
| Leading | 2353 | 107.17 | 100.99 | ↙ down-left（相對走弱） |
| Leading | 1319 | 107.1 | 103.67 | → up-left（動量轉弱） |
| Leading | 2882 | 107.07 | 103.8 | → up-left（動量轉弱） |
| Leading | 2887 | 106.81 | 105.47 | ↗ up-right（相對走強） |
| Leading | 2881 | 106.74 | 102.73 | → up-left（動量轉弱） |
| Leading | 2324 | 105.84 | 100.13 | ↙ down-left（相對走弱） |
| Leading | 8021 | 105.49 | 104.79 | ↗ up-right（相對走強） |
| Leading | 6196 | 105.23 | 100.57 | ↗ up-right（相對走強） |
| Leading | 2891 | 105.13 | 102.31 | → up-left（動量轉弱） |
| Leading | 5536 | 104.59 | 102.24 | ↗ up-right（相對走強） |
| Improving | 3653 | 99.87 | 106.34 | → up-left（動量轉弱） |
| Improving | 6515 | 95.74 | 103.46 | ↗ up-right（相對走強） |
| Improving | 6177 | 99.41 | 103.4 | ↗ up-right（相對走強） |
| Improving | 1795 | 97.31 | 103.4 | ↗ up-right（相對走強） |
| Improving | 6510 | 96.98 | 103.37 | ↗ up-right（相對走強） |
| Improving | 6625 | 99.68 | 102.31 | ↗ up-right（相對走強） |
| Improving | 3665 | 91.34 | 102.21 | ↗ up-right（相對走強） |
| Improving | 5880 | 99.6 | 102.14 | ↗ up-right（相對走強） |
| Improving | 5876 | 99.97 | 101.96 | ↗ up-right（相對走強） |
| Improving | 6191 | 98.43 | 101.85 | → up-left（動量轉弱） |
| Improving | 6505 | 98.73 | 101.81 | → up-left（動量轉弱） |
| Improving | 2006 | 98.76 | 101.77 | ↗ up-right（相對走強） |
| Weakening | 2327 | 116.29 | 96.71 | ↙ down-left（相對走弱） |
| Weakening | 2472 | 111.45 | 97.01 | ↙ down-left（相對走弱） |
| Weakening | 2344 | 108.68 | 99.03 | ↑ down-left（相對改善） |
| Weakening | 3008 | 106.44 | 99.34 | ↗ up-right（相對走強） |
| Weakening | 6239 | 106.23 | 96.68 | ↙ down-left（相對走弱） |
| Weakening | 2356 | 105.97 | 97.2 | ↙ down-left（相對走弱） |
| Weakening | 8358 | 105.52 | 98.1 | ↙ down-left（相對走弱） |
| Weakening | 2357 | 105.36 | 99.78 | ↙ down-left（相對走弱） |
| Weakening | 2408 | 105.04 | 99.12 | ↗ up-right（相對走強） |
| Weakening | 4958 | 104.6 | 99.33 | ↗ up-right（相對走強） |
| Weakening | 2377 | 104.42 | 98.67 | ↙ down-left（相對走弱） |
| Weakening | 3189 | 104.06 | 96.6 | ↙ down-left（相對走弱） |
| Lagging | 8210 | 99.48 | 99.97 | ↗ up-right（相對走強） |
| Lagging | 2385 | 99.58 | 99.96 | ↙ down-left（相對走弱） |
| Lagging | 3044 | 98.46 | 99.9 | ↗ up-right（相對走強） |
| Lagging | 5347 | 97.62 | 99.69 | ↗ up-right（相對走強） |
| Lagging | 3264 | 97.32 | 99.49 | ↗ up-right（相對走強） |
| Lagging | 2383 | 98.61 | 99.45 | ↗ up-right（相對走強） |
| Lagging | 2308 | 99.37 | 99.34 | ↙ down-left（相對走弱） |
| Lagging | 3017 | 96.59 | 99.3 | ↙ down-left（相對走弱） |
| Lagging | 1815 | 94.17 | 99.25 | ↙ down-left（相對走弱） |
| Lagging | 6223 | 99.8 | 99.23 | ↗ up-right（相對走強） |
| Lagging | 3034 | 98.57 | 99.19 | ↗ up-right（相對走強） |
| Lagging | 1590 | 93.67 | 99.15 | ↗ up-right（相對走強） |

![RRG scatter · Kempenaer](axis/rrg/scatter.svg)


## 4 · Minervini Trend Template · universe pass rate


> **Minervini Trend Template · universe pass rate**：對樣本個股逐日檢查八項模板，≥7/8 計入（RS 項略過）。可與 % above 200-day MA 對照：廣度高且 pass rate 高 → 廣泛 Stage 2 參與；廣度高但 pass rate 低 → 可能少數 leadership 拉指數。
>
> **圖表：** 近 90 日每日 pass rate。

**Pass rate 50.0%** · bulk scan ≥7/8 (RS omitted)

**Notes**

樣本 **157** 檔中 **50.0%** 通過 **Minervini Trend Template**（≥7/8，RS omitted）。 近 5 日上升 4.4pp。 % above MA 高且 pass rate >50% → 廣泛 Stage 2 參與。

| Reading | Value |
|---------|-------|
| Pass rate | 50.0% |
| 5d Δ | +4.4pp |
| Universe n | 157 |

![Minervini universe pass rate · 90d](axis/stage2/participation_spark.svg)


---
config: `config/regime.yaml` · 基準 IX0001 · 資料日 2026-06-15 · 快照 `snapshots/20260615/`
