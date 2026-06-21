# Market structure memo · 2026-06-18

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

IX0001 · Weinstein Stage 2 · advancing（上升）；% above 200-day MA 94.4%（Overbought · 過熱 (>80%)）；Zweig EMA rhythm 58.8%（High · 偏強 (≥58%)）；RRG Leading + Improving 44.6%；Minervini template pass rate 52.5%。 200-day 廣度偏高但 Minervini pass rate 仍高，漲幅擴散較廣。 綜合：200MA Level 偏高、Zweig EMA rhythm 中等偏強，但 Thrust 窗口未 active → 高位慣性，非剛點火。

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

**% above 200-day MA 94.4%**（Overbought · 過熱 (>80%)）；**% above 50-day MA 82.7%**。50 vs 200 spread **-11.8 pp**，50-day 低於 200-day。 50-day MA 近 5 日上升 12.0pp；200-day MA 近 5 日上升 2.7pp。 未見指數漲／50-day 廣度降之背離。 200-day 廣度處高位區間；屬環境描述，請搭配 Weinstein Stage 與 RRG 閱讀。

| Reading | Value |
|---------|-------|
| % above 200-day MA | 94.4% |
| % above 50-day MA | 82.7% |
| 5d Δ (50 / 200) | +12.0pp / +2.7pp |
| 50 vs 200 spread | -11.8pp |
| Advance/decline divergence | no |
| Universe n | 126 |

![% Above MA · index + breadth panel](axis/breadth/spark.svg)


### 1B · Zweig EMA rhythm tier


> **Zweig EMA rhythm tier**（Zweig / Deemer 廣度傳統）：adv/decl 日線 ratio 的 10-day EMA，依 tier 閾值分 off / low / mid / high。Research validation 顯示 rhythm tier 具統計增量；Regime 僅報讀 tier，**不含 exposure 仓位**。

**High · 偏強 (≥58%)**

**Notes**

**Zweig EMA rhythm tier** · adv/decl 10-day EMA **58.8%**（High · 偏強 (≥58%)）。 5d Δ 近 5 日上升 14.1pp。 Rhythm 偏強，代表 adv/decl 慣性高；仍須對照 Level 是否過熱。

| Reading | Value |
|---------|-------|
| Zweig adv/decl 10-day EMA | 58.8% |
| Rhythm tier | high |
| 5d Δ | +14.1pp |

![Zweig EMA rhythm · 90d](axis/breadth/zweig_ema_spark.svg)


### 1C · Breadth impulse · Zweig thrust / Deemer BAM


> **Impulse** 偵測 thrust 事件：Zweig 以 adv/decl EMA 穿越偵測 Breadth Thrust；Deemer 以 10-day adv/decl ratio 偵測 BAM。Thrust 窗口 active 表示近期曾觸發 thrust／BAM，仍在 hold 期內。

**Notes**

Thrust 窗口未 active

| Reading | Value |
|---------|-------|
| Deemer 10-day adv/decl | 1.09 |
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

**IX0001** 週線 **Stage 2 · advancing（上升）**：收盤 在 **30-week MA** 上，MA 斜率 +7.14%，偏離 30-week MA **+33.5%**，higher lows 成立。 偏離 MA 較大，屬 Stage 2 後段（Weinstein topping 觀察區，非賣出訊號）。 **Minervini Trend Template**（指數）**7/8** passed；Stage 2 型結構仍完整。

| Reading | Value |
|---------|-------|
| 30-week MA slope | 7.14% |
| Extension vs 30-week MA | 33.5% |
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

**Leading + Improving: 44.6%** · n=150

**Notes**

**Leading + Improving** 占樣本 **44.6%**（Leading 23.3% · Improving 21.3%）。Weakening 14.7% · Lagging 40.7%。 Lagging 占比高，留意 Improving → Leading 遷移。 1-day migration：Improving→Leading **0** · Leading→Weakening **1** · Lagging→Improving **3** · Weakening→Lagging **2**。

| Quadrant | Count | Share |
|----------|------:|------:|
| Leading | 35 | 23.3% |
| Improving | 32 | 21.3% |
| Weakening | 22 | 14.7% |
| Lagging | 61 | 40.7% |

| 1-day quadrant migration | Count |
|--------------------------|------:|
| Improving → Leading | 0 |
| Leading → Weakening | 1 |
| Lagging → Improving | 3 |
| Weakening → Lagging | 2 |

### RRG symbol table（Kempenaer · StockCharts）

依象限排序；RS-Ratio（JdK）· RS-Momentum · 4-day tail。

| Quadrant | Symbol | RS-Ratio | RS-Mom | Tail |
|----------|--------|----------|--------|------|
| Leading | 2344 | 110.93 | 100.95 | ↗ up-right（相對走強） |
| Leading | 3008 | 109.91 | 102.23 | ↗ up-right（相對走強） |
| Leading | 2059 | 109.82 | 104.0 | → up-left（動量轉弱） |
| Leading | 2883 | 108.69 | 104.16 | → up-left（動量轉弱） |
| Leading | 2882 | 108.67 | 103.6 | → up-left（動量轉弱） |
| Leading | 2887 | 108.39 | 104.66 | → up-left（動量轉弱） |
| Leading | 1303 | 107.92 | 104.42 | ↗ up-right（相對走強） |
| Leading | 2408 | 107.73 | 101.55 | ↗ up-right（相對走強） |
| Leading | 2881 | 107.67 | 102.42 | → up-left（動量轉弱） |
| Leading | 8996 | 106.57 | 106.18 | ↗ up-right（相對走強） |
| Leading | 4958 | 106.48 | 101.13 | ↗ up-right（相對走強） |
| Leading | 8021 | 106.03 | 103.59 | → up-left（動量轉弱） |
| Improving | 6515 | 98.57 | 105.0 | ↗ up-right（相對走強） |
| Improving | 3665 | 94.25 | 104.25 | ↗ up-right（相對走強） |
| Improving | 6510 | 98.7 | 103.93 | ↗ up-right（相對走強） |
| Improving | 6187 | 98.02 | 102.59 | ↗ up-right（相對走強） |
| Improving | 1785 | 96.59 | 102.44 | ↗ up-right（相對走強） |
| Improving | 3529 | 93.16 | 101.93 | ↗ up-right（相對走強） |
| Improving | 3443 | 96.15 | 101.58 | ↗ up-right（相對走強） |
| Improving | 2006 | 99.22 | 101.55 | → up-left（動量轉弱） |
| Improving | 6191 | 98.92 | 101.49 | → up-left（動量轉弱） |
| Improving | 2337 | 98.13 | 101.46 | ↗ up-right（相對走強） |
| Improving | 7751 | 90.71 | 101.4 | ↗ up-right（相對走強） |
| Improving | 3045 | 99.85 | 101.35 | → up-left（動量轉弱） |
| Weakening | 2327 | 115.44 | 97.08 | ↑ down-left（相對改善） |
| Weakening | 2472 | 107.8 | 95.15 | ↙ down-left（相對走弱） |
| Weakening | 8358 | 105.41 | 98.54 | ↑ down-left（相對改善） |
| Weakening | 6196 | 104.46 | 99.73 | ↙ down-left（相對走弱） |
| Weakening | 6239 | 104.0 | 95.72 | ↙ down-left（相對走弱） |
| Weakening | 2316 | 103.76 | 98.72 | ↗ up-right（相對走強） |
| Weakening | 2353 | 103.72 | 97.5 | ↙ down-left（相對走弱） |
| Weakening | 2324 | 102.77 | 97.19 | ↙ down-left（相對走弱） |
| Weakening | 2357 | 102.52 | 97.33 | ↙ down-left（相對走弱） |
| Weakening | 2303 | 102.48 | 96.14 | ↑ down-left（相對改善） |
| Weakening | 2404 | 102.39 | 99.85 | ↙ down-left（相對走弱） |
| Weakening | 2377 | 102.38 | 97.41 | ↙ down-left（相對走弱） |
| Lagging | 1519 | 95.88 | 99.83 | ↙ down-left（相對走弱） |
| Lagging | 6139 | 99.98 | 99.79 | ↙ down-left（相對走弱） |
| Lagging | 3264 | 97.17 | 99.77 | ↑ down-left（相對改善） |
| Lagging | 3023 | 98.86 | 99.68 | ↙ down-left（相對走弱） |
| Lagging | 6442 | 97.35 | 99.62 | ↙ down-left（相對走弱） |
| Lagging | 2345 | 96.81 | 99.62 | ↗ up-right（相對走強） |
| Lagging | 1815 | 94.04 | 99.36 | ↑ down-left（相對改善） |
| Lagging | 3661 | 94.27 | 99.33 | ↗ up-right（相對走強） |
| Lagging | 3583 | 95.81 | 99.3 | ↗ up-right（相對走強） |
| Lagging | 2474 | 99.23 | 99.21 | ↙ down-left（相對走弱） |
| Lagging | 8210 | 98.09 | 99.2 | ↙ down-left（相對走弱） |
| Lagging | 4441 | 96.58 | 99.08 | ↗ up-right（相對走強） |

![RRG scatter · Kempenaer](axis/rrg/scatter.svg)


## 4 · Minervini Trend Template · universe pass rate


> **Minervini Trend Template · universe pass rate**：對樣本個股逐日檢查八項模板，≥7/8 計入（RS 項略過）。可與 % above 200-day MA 對照：廣度高且 pass rate 高 → 廣泛 Stage 2 參與；廣度高但 pass rate 低 → 可能少數 leadership 拉指數。
>
> **圖表：** 近 90 日每日 pass rate。

**Pass rate 52.5%** · bulk scan ≥7/8 (RS omitted)

**Notes**

樣本 **150** 檔中 **52.5%** 通過 **Minervini Trend Template**（≥7/8，RS omitted）。 近 5 日上升 11.4pp。 % above MA 高且 pass rate >50% → 廣泛 Stage 2 參與。

| Reading | Value |
|---------|-------|
| Pass rate | 52.5% |
| 5d Δ | +11.4pp |
| Universe n | 150 |

![Minervini universe pass rate · 90d](axis/stage2/participation_spark.svg)


---
config: `config/regime.yaml` · 基準 IX0001 · 資料日 2026-06-18 · 快照 `snapshots/20260618/`
