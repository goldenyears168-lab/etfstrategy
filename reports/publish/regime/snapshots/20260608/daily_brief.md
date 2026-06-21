# Market structure memo · 2026-06-08

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

IX0001 · Weinstein Stage 2 · advancing（上升）；% above 200-day MA 91.0%（Overbought · 過熱 (>80%)）；Zweig EMA rhythm 41.5%（Off · 關閉 (<45%)）；RRG Leading + Improving 49.0%；Minervini template pass rate 45.6%。 200-day 廣度偏高但 Minervini pass rate 仍高，漲幅擴散較廣。

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

**% above 200-day MA 91.0%**（Overbought · 過熱 (>80%)）；**% above 50-day MA 75.9%**。50 vs 200 spread **-15.0 pp**，50-day 低於 200-day。 50-day MA 近 5 日下降 9.2pp；200-day MA 近 5 日上升 0.8pp。 未見指數漲／50-day 廣度降之背離。 200-day 廣度處高位區間；屬環境描述，請搭配 Weinstein Stage 與 RRG 閱讀。

| Reading | Value |
|---------|-------|
| % above 200-day MA | 91.0% |
| % above 50-day MA | 75.9% |
| 5d Δ (50 / 200) | -9.2pp / +0.8pp |
| 50 vs 200 spread | -15.0pp |
| Advance/decline divergence | no |
| Universe n | 133 |

![% Above MA · index + breadth panel](axis/breadth/spark.svg)


### 1B · Zweig EMA rhythm tier


> **Zweig EMA rhythm tier**（Zweig / Deemer 廣度傳統）：adv/decl 日線 ratio 的 10-day EMA，依 tier 閾值分 off / low / mid / high。Research validation 顯示 rhythm tier 具統計增量；Regime 僅報讀 tier，**不含 exposure 仓位**。

**Off · 關閉 (<45%)**

**Notes**

**Zweig EMA rhythm tier** · adv/decl 10-day EMA **41.5%**（Off · 關閉 (<45%)）。 5d Δ 近 5 日下降 16.5pp。 Rhythm 關閉區間，adv/decl 慣性極弱。

| Reading | Value |
|---------|-------|
| Zweig adv/decl 10-day EMA | 41.5% |
| Rhythm tier | off |
| 5d Δ | -16.5pp |

![Zweig EMA rhythm · 90d](axis/breadth/zweig_ema_spark.svg)


### 1C · Breadth impulse · Zweig thrust / Deemer BAM


> **Impulse** 偵測 thrust 事件：Zweig 以 adv/decl EMA 穿越偵測 Breadth Thrust；Deemer 以 10-day adv/decl ratio 偵測 BAM。Thrust 窗口 active 表示近期曾觸發 thrust／BAM，仍在 hold 期內。

**Notes**

Thrust 窗口未 active

| Reading | Value |
|---------|-------|
| Deemer 10-day adv/decl | 0.85 |
| Zweig thrust today | no |
| Deemer BAM today | no |
| Thrust window active | no |
| Days remaining | 0 / 42 |

## 2 · Weinstein Stage Analysis · weekly


> **Weinstein Stage Analysis（1988）** 以 **週線** 加權指數判 Stage：1 basing → 2 advancing → 3 topping → 4 declining；基準為 **30-week MA**。圖底 **Stage ribbon** 為週線 Stage 著色（紫 S1、綠 S2、橙 S3、紅 S4）。
>
> **Minervini Trend Template（2013）** 八條日線規則檢驗指數是否處 Stage 2 型上升結構。

### IX0001 · **Stage 2 · advancing（上升）**

**Notes**

**IX0001** 週線 **Stage 2 · advancing（上升）**：收盤 在 **30-week MA** 上，MA 斜率 +6.76%，偏離 30-week MA **+27.4%**，higher lows 成立。 偏離 MA 較大，屬 Stage 2 後段（Weinstein topping 觀察區，非賣出訊號）。 **Minervini Trend Template**（指數）**7/8** passed；Stage 2 型結構仍完整。

| Reading | Value |
|---------|-------|
| 30-week MA slope | 6.76% |
| Extension vs 30-week MA | 27.4% |
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

**Leading + Improving: 49.0%** · n=157

**Notes**

**Leading + Improving** 占樣本 **49.0%**（Leading 22.9% · Improving 26.1%）。Weakening 18.5% · Lagging 32.5%。 四象限分散，宜看 migration 與 symbol table，不宜只看最大象限。 1-day migration：Improving→Leading **3** · Leading→Weakening **5** · Lagging→Improving **8** · Weakening→Lagging **5**。

| Quadrant | Count | Share |
|----------|------:|------:|
| Leading | 36 | 22.9% |
| Improving | 41 | 26.1% |
| Weakening | 29 | 18.5% |
| Lagging | 51 | 32.5% |

| 1-day quadrant migration | Count |
|--------------------------|------:|
| Improving → Leading | 3 |
| Leading → Weakening | 5 |
| Lagging → Improving | 8 |
| Weakening → Lagging | 5 |

### RRG symbol table（Kempenaer · StockCharts）

依象限排序；RS-Ratio（JdK）· RS-Momentum · 4-day tail。

| Quadrant | Symbol | RS-Ratio | RS-Mom | Tail |
|----------|--------|----------|--------|------|
| Leading | 2472 | 115.91 | 100.96 | ↙ down-left（相對走弱） |
| Leading | 2356 | 112.09 | 103.84 | ↙ down-left（相對走弱） |
| Leading | 6239 | 111.26 | 101.53 | ↙ down-left（相對走弱） |
| Leading | 2344 | 110.9 | 100.81 | ↙ down-left（相對走弱） |
| Leading | 3189 | 109.82 | 102.34 | ↙ down-left（相對走弱） |
| Leading | 2324 | 109.3 | 106.27 | → up-left（動量轉弱） |
| Leading | 2353 | 108.67 | 105.17 | → up-left（動量轉弱） |
| Leading | 2481 | 108.39 | 100.56 | ↙ down-left（相對走弱） |
| Leading | 8358 | 108.32 | 100.43 | ↙ down-left（相對走弱） |
| Leading | 2357 | 108.16 | 104.16 | → up-left（動量轉弱） |
| Leading | 2377 | 106.52 | 100.78 | ↙ down-left（相對走弱） |
| Leading | 6271 | 106.44 | 100.75 | ↙ down-left（相對走弱） |
| Improving | 3653 | 95.84 | 107.39 | ↗ up-right（相對走強） |
| Improving | 6805 | 98.81 | 103.52 | ↗ up-right（相對走強） |
| Improving | 2880 | 98.55 | 103.35 | ↗ up-right（相對走強） |
| Improving | 6442 | 98.44 | 103.17 | ↗ up-right（相對走強） |
| Improving | 2890 | 98.3 | 102.75 | ↗ up-right（相對走強） |
| Improving | 6235 | 98.48 | 102.59 | → up-left（動量轉弱） |
| Improving | 6191 | 97.32 | 102.33 | ↗ up-right（相對走強） |
| Improving | 8996 | 98.03 | 102.15 | ↗ up-right（相對走強） |
| Improving | 3293 | 99.14 | 102.11 | ↗ up-right（相對走強） |
| Improving | 6505 | 97.75 | 102.08 | ↗ up-right（相對走強） |
| Improving | 1795 | 94.01 | 101.8 | ↗ up-right（相對走強） |
| Improving | 3017 | 98.53 | 101.75 | → up-left（動量轉弱） |
| Weakening | 2327 | 120.16 | 99.57 | ↙ down-left（相對走弱） |
| Weakening | 6147 | 109.64 | 97.04 | ↙ down-left（相對走弱） |
| Weakening | 2303 | 107.58 | 95.87 | ↙ down-left（相對走弱） |
| Weakening | 2454 | 106.59 | 96.07 | ↙ down-left（相對走弱） |
| Weakening | 2408 | 106.28 | 99.3 | ↙ down-left（相對走弱） |
| Weakening | 2316 | 106.22 | 99.3 | ↙ down-left（相對走弱） |
| Weakening | 3008 | 105.96 | 98.74 | ↙ down-left（相對走弱） |
| Weakening | 6274 | 104.72 | 97.35 | ↙ down-left（相對走弱） |
| Weakening | 6415 | 104.37 | 96.27 | ↙ down-left（相對走弱） |
| Weakening | 4958 | 104.11 | 97.69 | ↙ down-left（相對走弱） |
| Weakening | 6488 | 103.81 | 97.6 | ↙ down-left（相對走弱） |
| Weakening | 8150 | 102.42 | 99.66 | ↙ down-left（相對走弱） |
| Lagging | 3105 | 96.39 | 99.95 | ↙ down-left（相對走弱） |
| Lagging | 2330 | 98.47 | 99.93 | ↗ up-right（相對走強） |
| Lagging | 6584 | 95.73 | 99.91 | ↙ down-left（相對走弱） |
| Lagging | 3023 | 99.15 | 99.88 | ↗ up-right（相對走強） |
| Lagging | 4967 | 97.43 | 99.85 | ↗ up-right（相對走強） |
| Lagging | 2027 | 99.95 | 99.79 | ↙ down-left（相對走弱） |
| Lagging | 6510 | 93.14 | 99.69 | ↗ up-right（相對走強） |
| Lagging | 2449 | 97.35 | 99.59 | ↙ down-left（相對走弱） |
| Lagging | 1216 | 96.78 | 99.59 | ↗ up-right（相對走強） |
| Lagging | 2368 | 96.0 | 99.37 | ↗ up-right（相對走強） |
| Lagging | 6187 | 94.33 | 99.34 | ↗ up-right（相對走強） |
| Lagging | 4441 | 97.93 | 99.19 | ↑ down-left（相對改善） |

![RRG scatter · Kempenaer](axis/rrg/scatter.svg)


## 4 · Minervini Trend Template · universe pass rate


> **Minervini Trend Template · universe pass rate**：對樣本個股逐日檢查八項模板，≥7/8 計入（RS 項略過）。可與 % above 200-day MA 對照：廣度高且 pass rate 高 → 廣泛 Stage 2 參與；廣度高但 pass rate 低 → 可能少數 leadership 拉指數。
>
> **圖表：** 近 90 日每日 pass rate。

**Pass rate 45.6%** · bulk scan ≥7/8 (RS omitted)

**Notes**

樣本 **158** 檔中 **45.6%** 通過 **Minervini Trend Template**（≥7/8，RS omitted）。 近 5 日下降 7.0pp。

| Reading | Value |
|---------|-------|
| Pass rate | 45.6% |
| 5d Δ | -7.0pp |
| Universe n | 158 |

![Minervini universe pass rate · 90d](axis/stage2/participation_spark.svg)


---
config: `config/regime.yaml` · 基準 IX0001 · 資料日 2026-06-08 · 快照 `snapshots/20260608/`
