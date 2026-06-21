# Market structure memo · 2026-06-12

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

IX0001 · Weinstein Stage 2 · advancing（上升）；% above 200-day MA 91.0%（Overbought · 過熱 (>80%)）；Zweig EMA rhythm 50.8%（Mid · 中等 (50–58%)）；RRG Leading + Improving 48.1%；Minervini template pass rate 43.7%。 50-day 廣度低於 200-day，短線擴散略收窄。 綜合：200MA Level 偏高、Zweig EMA rhythm 中等偏強，但 Thrust 窗口未 active → 高位慣性，非剛點火。

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

**% above 200-day MA 91.0%**（Overbought · 過熱 (>80%)）；**% above 50-day MA 73.9%**。50 vs 200 spread **-17.1 pp**，50-day 低於 200-day。 50-day MA 近 5 日下降 13.5pp；200-day MA 近 5 日下降 2.3pp。 未見指數漲／50-day 廣度降之背離。 200-day 廣度處高位區間；屬環境描述，請搭配 Weinstein Stage 與 RRG 閱讀。

| Reading | Value |
|---------|-------|
| % above 200-day MA | 91.0% |
| % above 50-day MA | 73.9% |
| 5d Δ (50 / 200) | -13.5pp / -2.3pp |
| 50 vs 200 spread | -17.1pp |
| Advance/decline divergence | no |
| Universe n | 133 |

![% Above MA · index + breadth panel](axis/breadth/spark.svg)


### 1B · Zweig EMA rhythm tier


> **Zweig EMA rhythm tier**（Zweig / Deemer 廣度傳統）：adv/decl 日線 ratio 的 10-day EMA，依 tier 閾值分 off / low / mid / high。Research validation 顯示 rhythm tier 具統計增量；Regime 僅報讀 tier，**不含 exposure 仓位**。

**Mid · 中等 (50–58%)**

**Notes**

**Zweig EMA rhythm tier** · adv/decl 10-day EMA **50.8%**（Mid · 中等 (50–58%)）。 5d Δ 近 5 日上升 1.6pp。 Rhythm 中等，市場參與節奏尚可。

| Reading | Value |
|---------|-------|
| Zweig adv/decl 10-day EMA | 50.8% |
| Rhythm tier | mid |
| 5d Δ | +1.6pp |

![Zweig EMA rhythm · 90d](axis/breadth/zweig_ema_spark.svg)


### 1C · Breadth impulse · Zweig thrust / Deemer BAM


> **Impulse** 偵測 thrust 事件：Zweig 以 adv/decl EMA 穿越偵測 Breadth Thrust；Deemer 以 10-day adv/decl ratio 偵測 BAM。Thrust 窗口 active 表示近期曾觸發 thrust／BAM，仍在 hold 期內。

**Notes**

Thrust 窗口未 active

| Reading | Value |
|---------|-------|
| Deemer 10-day adv/decl | 0.93 |
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

**IX0001** 週線 **Stage 2 · advancing（上升）**：收盤 在 **30-week MA** 上，MA 斜率 +6.83%，偏離 30-week MA **+29.3%**，higher lows 成立。 偏離 MA 較大，屬 Stage 2 後段（Weinstein topping 觀察區，非賣出訊號）。 **Minervini Trend Template**（指數）**7/8** passed；Stage 2 型結構仍完整。

| Reading | Value |
|---------|-------|
| 30-week MA slope | 6.83% |
| Extension vs 30-week MA | 29.26% |
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

**Leading + Improving: 48.1%** · n=156

**Notes**

**Leading + Improving** 占樣本 **48.1%**（Leading 23.7% · Improving 24.4%）。Weakening 16.0% · Lagging 35.9%。 四象限分散，宜看 migration 與 symbol table，不宜只看最大象限。 1-day migration：Improving→Leading **2** · Leading→Weakening **0** · Lagging→Improving **2** · Weakening→Lagging **0**。

| Quadrant | Count | Share |
|----------|------:|------:|
| Leading | 37 | 23.7% |
| Improving | 38 | 24.4% |
| Weakening | 25 | 16.0% |
| Lagging | 56 | 35.9% |

| 1-day quadrant migration | Count |
|--------------------------|------:|
| Improving → Leading | 2 |
| Leading → Weakening | 0 |
| Lagging → Improving | 2 |
| Weakening → Lagging | 0 |

### RRG symbol table（Kempenaer · StockCharts）

依象限排序；RS-Ratio（JdK）· RS-Momentum · 4-day tail。

| Quadrant | Symbol | RS-Ratio | RS-Mom | Tail |
|----------|--------|----------|--------|------|
| Leading | 2059 | 108.46 | 104.85 | ↗ up-right（相對走強） |
| Leading | 2353 | 107.57 | 101.73 | ↙ down-left（相對走弱） |
| Leading | 2883 | 107.11 | 105.44 | ↗ up-right（相對走強） |
| Leading | 1319 | 106.93 | 104.07 | ↗ up-right（相對走強） |
| Leading | 2882 | 106.82 | 104.14 | ↗ up-right（相對走強） |
| Leading | 2324 | 106.67 | 101.21 | ↙ down-left（相對走弱） |
| Leading | 2881 | 106.56 | 102.99 | ↗ up-right（相對走強） |
| Leading | 2357 | 106.3 | 100.79 | ↙ down-left（相對走弱） |
| Leading | 2887 | 105.99 | 105.44 | ↗ up-right（相對走強） |
| Leading | 8021 | 105.43 | 105.33 | ↗ up-right（相對走強） |
| Leading | 2891 | 105.08 | 102.64 | → up-left（動量轉弱） |
| Leading | 6196 | 104.79 | 100.27 | ↗ up-right（相對走強） |
| Improving | 3653 | 99.44 | 106.96 | → up-left（動量轉弱） |
| Improving | 1795 | 97.12 | 103.65 | ↗ up-right（相對走強） |
| Improving | 6177 | 98.92 | 103.25 | ↗ up-right（相對走強） |
| Improving | 6510 | 96.09 | 102.73 | ↗ up-right（相對走強） |
| Improving | 6625 | 99.44 | 102.37 | ↗ up-right（相對走強） |
| Improving | 5880 | 99.4 | 102.21 | ↗ up-right（相對走強） |
| Improving | 6515 | 94.15 | 102.0 | ↗ up-right（相對走強） |
| Improving | 6505 | 98.62 | 101.95 | ↗ up-right（相對走強） |
| Improving | 5876 | 99.7 | 101.91 | ↗ up-right（相對走強） |
| Improving | 6805 | 98.71 | 101.89 | ↙ down-left（相對走弱） |
| Improving | 9904 | 98.7 | 101.87 | ↗ up-right（相對走強） |
| Improving | 2368 | 98.64 | 101.84 | ↗ up-right（相對走強） |
| Weakening | 2327 | 116.76 | 96.83 | ↙ down-left（相對走弱） |
| Weakening | 2472 | 112.21 | 97.48 | ↙ down-left（相對走弱） |
| Weakening | 2344 | 108.12 | 98.44 | ↙ down-left（相對走弱） |
| Weakening | 6239 | 107.54 | 97.69 | ↙ down-left（相對走弱） |
| Weakening | 2356 | 107.36 | 98.36 | ↙ down-left（相對走弱） |
| Weakening | 3008 | 106.11 | 98.96 | ↗ up-right（相對走強） |
| Weakening | 8358 | 105.75 | 98.17 | ↙ down-left（相對走弱） |
| Weakening | 3189 | 105.26 | 97.51 | ↙ down-left（相對走弱） |
| Weakening | 2377 | 104.86 | 99.0 | ↙ down-left（相對走弱） |
| Weakening | 2408 | 104.67 | 98.61 | ↙ down-left（相對走弱） |
| Weakening | 2454 | 104.18 | 96.12 | ↙ down-left（相對走弱） |
| Weakening | 4958 | 104.16 | 98.77 | ↑ down-left（相對改善） |
| Lagging | 2308 | 99.9 | 99.83 | ↙ down-left（相對走弱） |
| Lagging | 3017 | 97.07 | 99.79 | ↙ down-left（相對走弱） |
| Lagging | 8210 | 99.48 | 99.77 | ↗ up-right（相對走強） |
| Lagging | 3044 | 98.13 | 99.49 | ↗ up-right（相對走強） |
| Lagging | 1815 | 94.37 | 99.41 | ↙ down-left（相對走弱） |
| Lagging | 2313 | 98.8 | 99.3 | ↗ up-right（相對走強） |
| Lagging | 2383 | 98.61 | 99.3 | ↗ up-right（相對走強） |
| Lagging | 5439 | 94.27 | 99.13 | ↑ down-left（相對改善） |
| Lagging | 2367 | 97.33 | 99.1 | ↗ up-right（相對走強） |
| Lagging | 3264 | 97.1 | 99.09 | ↗ up-right（相對走強） |
| Lagging | 6139 | 99.32 | 99.0 | ↙ down-left（相對走弱） |
| Lagging | 3034 | 98.57 | 99.0 | ↗ up-right（相對走強） |

![RRG scatter · Kempenaer](axis/rrg/scatter.svg)


## 4 · Minervini Trend Template · universe pass rate


> **Minervini Trend Template · universe pass rate**：對樣本個股逐日檢查八項模板，≥7/8 計入（RS 項略過）。可與 % above 200-day MA 對照：廣度高且 pass rate 高 → 廣泛 Stage 2 參與；廣度高但 pass rate 低 → 可能少數 leadership 拉指數。
>
> **圖表：** 近 90 日每日 pass rate。

**Pass rate 43.7%** · bulk scan ≥7/8 (RS omitted)

**Notes**

樣本 **157** 檔中 **43.7%** 通過 **Minervini Trend Template**（≥7/8，RS omitted）。 近 5 日下降 9.5pp。

| Reading | Value |
|---------|-------|
| Pass rate | 43.7% |
| 5d Δ | -9.5pp |
| Universe n | 157 |

![Minervini universe pass rate · 90d](axis/stage2/participation_spark.svg)


---
config: `config/regime.yaml` · 基準 IX0001 · 資料日 2026-06-12 · 快照 `snapshots/20260612/`
