# 1 分 K 短期獲利策略目錄 · 1-Minute Intraday Strategy Catalog

| Field | Value |
|-------|-------|
| Version | 2026-06-27 |
| Layer | Research layer（探索參考）· 非 `config/strategy.yaml` 採納規格 |
| Scope | 以 **1 分 K** 為執行或訊號週期的短期獲利模型；各策略保留**最原始作法**，可獨立使用 |
| 免責 | 外部方法論蒐集；未經本專案回測驗證；實盤需自行評估成本、滑價、法規與資料需求 |
| 回測文獻 | 有公開回測的 peer-reviewed 研究見 [`1m-intraday-strategy-backtest-literature.md`](1m-intraday-strategy-backtest-literature.md) |

---

## 目錄

1. [學術／量化模型](#1-學術量化模型)
2. [經典書籍作者](#2-經典書籍作者)
3. [TradingView 已發布策略](#3-tradingview-已發布策略)
4. [技術指標型 1 分 Scalping 模板](#4-技術指標型-1-分-scalping-模板)
5. [Order Flow / 微結構](#5-order-flow--微結構)
6. [依尋找標的邏輯分類](#6-依尋找標的邏輯分類)
7. [各派核心差異](#7-各派核心差異)
8. [實務提醒](#8-實務提醒)
9. [參考連結](#9-參考連結)
10. [後續可細化項目](#10-後續可細化項目)

---

## 1. 學術／量化模型

多數以 **tick、LOB（limit order book，限價委託簿）或 1 分 bar** 為最小時間單位。

| 模型 | 代表文獻／作者 | 核心邏輯 | 1 分 K 角色 |
|------|--------------|---------|------------|
| **LOB + 深度學習方向預測** | TFF-CL-GRU（MDPI *Applied Sciences*, 2024） | LOB / order flow 特徵預測下一刻價格方向 | 可聚合為 1 分 bar 或直接用 tick |
| **DRL 高頻主動交易** | PPO on LOB（arXiv 2101.07107） | RL agent 在 LOB 狀態下決定掛單／吃單 | 訓練資料為 tick 級 LOB |
| **DDQL 訊號執行** | LSTM + Duelling DQN（arXiv 2301.08688） | 把方向訊號轉成限價單執行策略 | 10 秒前瞻 horizon |
| **HMM-SVM 高頻 regime 分類** | Springer 2025 | 辨識 intraday 波動 regime，再切換策略 | 分鐘級 microstructure |
| **Avellaneda–Stoikov 做市** | Avellaneda & Stoikov (2008) | 依庫存風險動態調整 bid/ask | 實務常以 1 分 candle 更新 σ |
| **Stat Arb / Pairs（配對交易）** | Avellaneda PLATA 等 | 殘差均值回歸，做多空配對 | 1 分 bar 計算 spread z-score |
| **ORB 波動率狀態研究** | Holmberg et al. (2013) | ORB 在**高波動 regime** 才有 edge | 開盤 range 可設 1/5/15 分 |
| **Stocks in Play ORB** | Concretum Research / SFI (2024) | 5 分 ORB + 相對量篩選「當日異常活躍股」 | 執行可用 1 分 K 進出 |

### 1.1 Avellaneda–Stoikov 做市（原始公式摘要）

- **Reservation price（保留價）**：`r = s − q·γ·σ²·(T−t)`
- **Half-spread（半價差）**：`δ = (γ·σ²·(T−t)/2) + (1/γ)·ln(1 + γ/κ)`
- `s` = mid-price；`q` = 庫存；`γ` = 風險厭惡；`σ` = 波動率；`κ` = 訂單到達率

---

## 2. 經典書籍作者

### 2.1 Toby Crabel — Opening Range Breakout（ORB，開盤區間突破）

**書：** *Day Trading with Short Term Price Patterns and Opening Range Breakout* (1990)

**原始模型：**

- 開盤前 N 分鐘（含 **1 分**）定義 Opening Range（開盤區間）
- **Stretch（延伸值）**：過去 10 日「開→高」與「開→低」較小值取平均
- 買停：開盤 + stretch；賣停：開盤 − stretch
- 搭配 **NR4（窄區間第 4 日）**、**Inside Day（內包日）** 等日線 pattern 過濾

**1 分 K 用法：** 原文測過 1/5/10/15/30 分 opening range；現代 hyper-scalping 常壓到 **1 分 ORB**。

**後續研究：**

- Holmberg et al.：高波動 state 下 ORB 日報酬顯著優於低波動（原油 ~200 bps/日、S&P500 ~150 bps/日差）
- Concretum (2024)：5 分 ORB + Stocks in Play + 相對量 ≥100% → 淨報酬 Sharpe 2.81（2016–2023 美股）

---

### 2.2 Linda Raschke & Laurence Connors — *Street Smarts*

**原始模型（可映射到 1 分執行）：**

| Pattern | 邏輯 |
|---------|------|
| **NR4 / NR7** | 前一日為近 4/7 日最窄 range → 次日易出大 move，用 1 分 K 抓 breakout |
| **Turtle Soup** | 假突破前日高/低後反轉 |
| **3-Bar Play** | 強勢 bar + 整理 + 突破第三根 |
| **Hook Reversal** | 長影線反轉 + 確認 bar |

執行多在 **1–5 分 K**，1 分用於精準進場。

---

### 2.3 Al Brooks — Price Action Scalping

**代表：** *Trading Price Action* 系列

**原始 scalping 規則：**

- 建議最低 **2 分 K**（明確表示 1 分決策時間不足）
- 目標：Emini 至少 **1 point**；Forex 至少 **10 pips**
- 進場：**stop order** 賭 breakout 延續；**limit order** 賭 breakout 失敗反轉
- 讀 **ii pattern、double top/bottom、strong bar** 等 micro structure

若硬用 1 分 K，邏輯相同，但 Brooks 本人不推薦多數人這樣做。

---

### 2.4 Andrew Aziz — 1-Minute ORB

**來源：** Bear Bull Traders / TradingSim 整理

**Long 五條件：**

1. Gap ≥ 2%（視市值調整）
2. 流動性足夠
3. 方向趨勢清楚
4. 開盤在 VWAP / 均線有支撐
5. 突破**第一根 1 分 K** 的 body 或 high

**止損：** 第一根 1 分 K 的 low  
**目標：** 至少 1:1 risk/reward

---

### 2.5 Ross Cameron (Warrior Trading) — 小盤動量

**書：** *How to Day Trade*

**以 1 分 K 執行的原始 setup：**

| Setup | 原始進場 |
|-------|---------|
| **Gap and Go** | 9:30 買入第一根 1 分 K high 或 pre-market high 突破 |
| **Bull Flag** | 強勢旗桿 → 整理 → 第一根突破 pullback high 的 1 分 K |
| **Flat Top Breakout** | 多次測試同一阻力 → 1 分突破進場 |
| **ABCD Pattern** | A→B 漲 → C 回檔 → D 突破 B 點 |
| **HOD Momentum** | 掃描器觸發後，1 分 K 追價進場 |

**選股原始條件：**

| 參數 | 設定 |
|------|------|
| Gap % | ≥ 4% |
| 價格 | $2–$20 |
| Float | < 100M（理想 < 20M） |
| 相對量 RVOL | ≥ 1.5x |
| Pre-market 量 | ≥ 100,000 shares |
| 日均量 | ≥ 500,000 |

**風控：** profit cushion 先小倉試水；三連敗或達日虧上限即停。

---

### 2.6 ICT (Inner Circle Trader) — Smart Money Concepts（SMC）

多數用 5M/15M 定結構，**1M 做精準進場**。

| Model | 1 分 K 角色 |
|-------|------------|
| **Judas Swing** | 開盤假突破 sweep liquidity 後反轉；1M 看 MSS（market structure shift） |
| **Silver Bullet** | 10:00–11:00 ET（及 3–4AM、2–3PM）時間窗內，1M 找 FVG（fair value gap）回測進場 |
| **Venom Model** | 9:30–10:00 定 ORH/ORL → sweep → 1st Presented FVG 50% CE 進場 |
| **IOFED** | HTF FVG 區內，等 1M swing 形成後，第一根突破該 swing 的 1M close 進場 |
| **2022 Model** | Daily bias + AMD cycle；1M 確認 manipulation body 收回 range 內 |
| **Expansion / Range 雙模式** | Expansion：HTF displacement 後 1M 順勢 CHoCH；Range：equal highs/lows sweep 後 1M 反轉 |

**Silver Bullet 時間窗（ET）：**

| 窗口 | 時段 | 備註 |
|------|------|------|
| London | 3:00–4:00 AM | EUR/GBP 為主 |
| NY AM | 10:00–11:00 AM | 最高機率 |
| NY PM | 2:00–3:00 PM | 收盤前 delivery |

**SMC 1 分 scalping 標準流程：**

1. 開盤前 5 分鐘只觀察，標記 opening range
2. 15M bias 確立（多/空/不交易）
3. 標記 liquidity levels（session H/L、equal highs/lows、opening range）
4. Liquidity sweep → displacement → FVG / order block
5. 1M candlestick 確認（hammer、engulfing 等）
6. 止損在 sweep extreme 外；目標下一 liquidity pool；RR ≥ 2:1

---

## 3. TradingView 已發布策略

| 策略名 | 作者 | 原始邏輯 | 連結 |
|--------|------|---------|------|
| **Algo Torma ORB** | AlgoTorma | 5 分定 ORB → 1 分 breakout → limit 回測 ORB 位進場 | [TradingView](https://www.tradingview.com/script/BECdPF4r-Algo-Torma-ORB-strategy/) |
| **NQ Scalping ORB + VWAP Bias** | bradenstrock | ORB 突破 + VWAP 方向過濾 + ATR bracket | [TradingView](https://www.tradingview.com/script/b7IJ7mmW-NQ-Scalping-ORB-VWAP-Bias-ATR-Brackets/) |
| **VWAP ORB Pullback** | TraderTed420 | ORB 突破 → 回測 VWAP + 9 EMA 確認 | [TradingView](https://www.tradingview.com/script/75epRRh2-VWAP-ORB-Pullback-Strategy/) |
| **Ross GPT Momentum Scalp 1m** | rikhilrozario | VWAP + EMA9/20/50 + MACD + 5 根中 3 根陽線 | [TradingView](https://www.tradingview.com/script/re15NYVP-Ross-GPT-Momentum-Scalp-1m/) |
| **Bollinger + RSI Mean Reversion** | thechadyogi (Krishna Peri) | RSI 超買超賣 + BB 內反轉 candle | [TradingView](https://www.tradingview.com/script/XRPeqEdA-Bollinger-Bands-Mean-Reversion-using-RSI-Krishna-Peri/) |

---

## 4. 技術指標型 1 分 Scalping 模板

無單一原作者；TradingView / 教學社群最常獨立使用的原始模板。

| # | 策略 | 原始設定 | 進場 | 出場 |
|---|------|---------|------|------|
| 1 | **VWAP + MACD** | VWAP bias；MACD(12,26,9) | 價在 VWAP 上 + MACD 金叉 | MACD 死叉或回 VWAP |
| 2 | **EMA Pullback** | EMA 9/20/50 多頭排列 | 回測 EMA9 反彈 | 前高或 5–10 pips |
| 3 | **VWAP Mean Reversion** | 價偏離 VWAP > X σ | 限價在 VWAP ± band | 回到 VWAP |
| 4 | **Bollinger + RSI** | BB(20,2) + RSI(4) 或 RSI(14) | 觸下軌 + RSI<20 做多 | 中軌或 RSI 50 |
| 5 | **BB Squeeze Breakout** | BandWidth 壓縮至極低 | 突破 band + body ≥ 60% range | 對側 band |
| 6 | **Keltner + RSI** | KC(20,1.5) + RSI(14) | 觸 channel + RSI 極值 | 回 channel 中線 |
| 7 | **ALMA + Stochastic** | ALMA 趨勢 + Stoch(5,3,3) | 趨勢方向 + Stoch 交叉 | 固定 pip 或 Stoch 反向 |
| 8 | **RSI Divergence Scalp** | 1M 價新高但 RSI 未新高 | divergence 確認 bar | 固定 TP/SL |
| 9 | **Momentum Scalping** | 量增 + 突破前 15–30M S/R | 1M 突破進場 | momentum 減弱即出 |
| 10 | **Range Scalping** | 1M 定義 10–15 pip range | 下緣買、上緣賣 | range 外 stop |
| 11 | **S/R Scalping** | 15–30M 定 S/R | 1M 到達 + rejection candle | 回 VWAP 或對側 |
| 12 | **News Scalping** | 重大數據後 | **不追第一根**，等 1M 二次結構 | 快進快出 |

### 4.1 五類通用 Scalping Setup（technical-analysis-pro 整理）

| Setup | 步驟摘要 |
|-------|---------|
| **Momentum Scalping** | 量增突破 → 順向進 → TP 5–10 pips → SL 3–5 pips |
| **Pullback Scalping** | 價 > EMA20 → 回 EMA9 → 做多 → SL 在 EMA20 下 |
| **S/R Scalping** | HTF S/R → 1M 接近 + rejection → 逆勢短打 |
| **Range Scalping** | 定義 range → 下緣買上緣賣 → range 破即停 |
| **Breakout Scalping** | 5M 三角/窄 range → 1M 突破 → SL 在 pattern 內 |

### 4.2 1 分 K 常用指標參數（非預設值）

| 指標 | 1 分建議 | 說明 |
|------|---------|------|
| RSI | 4 或 7 | 預設 14 對 1 分太慢 |
| EMA | 9 / 21 | 快趨勢 |
| MACD | 12,26,9 或更快 | 動量確認 |
| Bollinger | 20,2 或 12,2 | 部分 FX 用 12 period |
| VWAP | 當日累積 | 機構參考價 |

---

## 5. Order Flow / 微結構

原始資料為 **tick / LOB**；1 分 K 為時間聚合容器。

| 模型 | 工具／作者脈絡 | 原始訊號 |
|------|--------------|---------|
| **Delta Absorption** | Bookmap / Sierra / ATAS | 大量 aggressive sell 但價不跌 → 被動買方吸收 → 做多 |
| **Delta Divergence** | CVD（cumulative volume delta） | 價創新高但 delta 未創新高 → 反轉 |
| **Liquidity Vacuum** | Bookmap heatmap | 掛單簿稀薄區 → 價快速穿越 |
| **Trapped Traders** | Footprint cluster | 高量區被反向突破 → 止損連鎖 |
| **Iceberg Detection** | Bookmap Large Lot Tracker | 隱藏大單在 key level 反覆出現 |
| **Bid/Ask Imbalance** | DOM Pro | 買賣量嚴重失衡 → 短線方向 |
| **Microprice Scalping** | Stoikov (2018) | `(bid×ask_sz + ask×bid_sz) / total_sz` 偏離 mid → 方向 |

**Footprint 1 分 bar 類型：** time interval、reversal bar、range bar、volume bar。

---

## 6. 依尋找標的邏輯分類

```
短期獲利模型
├── A. 動量延續（Momentum Continuation）
│   ├── 1-Min ORB（Crabel / Aziz）
│   ├── 5-Min ORB 定區 + 1-Min 執行（Concretum, AlgoTorma）
│   ├── Gap and Go / Bull Flag（Ross Cameron）
│   ├── Breakout Scalping（5M pattern → 1M 突破）
│   └── BB Squeeze Breakout
│
├── B. 均值回歸（Mean Reversion）
│   ├── VWAP Reversion
│   ├── Bollinger + RSI
│   ├── Range Scalping
│   ├── S/R Fade（rejection candle）
│   └── Stat Arb spread z-score
│
├── C. 假突破 / 反轉（Liquidity / Reversal）
│   ├── ICT Judas Swing / Venom / IOFED
│   ├── Liquidity Sweep + CHoCH（SMC）
│   ├── Turtle Soup（Raschke-Connors）
│   ├── Al Brooks limit order fade
│   └── Delta Divergence / Absorption
│
├── D. 時間窗 / 事件驅動（Time / Event）
│   ├── ICT Silver Bullet（3–4AM / 10–11AM / 2–3PM ET）
│   ├── Opening 5-min observe → 1-min trade（SMC 標準流程）
│   ├── News Scalping（第二根結構）
│   └── Session open kill zone（London / NY）
│
└── E. 量化 / 做市（Quant / MM）
    ├── Avellaneda-Stoikov market making
    ├── DRL LOB agent
    ├── LOB deep learning direction
    └── Pairs / ETF stat arb
```

---

## 7. 各派核心差異

| 維度 | 動量派（ORB/Gap） | 均值回歸派（BB/VWAP） | SMC/ICT 派 | Order Flow 派 | 量化派 |
|------|-----------------|-------------------|-----------|-------------|--------|
| **假設** | 開盤方向會延續 | 價格偏離會回歸 | 機構先 sweep 再 delivery | 成交壓力預示方向 | 統計 edge 可重複 |
| **標的篩選** | gap、RVOL、catalyst | 不限（range 市場佳） | liquidity pool 位置 | 高流動性期貨/FX | 配對/籃子 |
| **1M 用途** | 進出場 trigger | 超買超賣確認 | 精準 entry/stop | footprint bar | 特徵聚合 |
| **持倉** | 數秒–15 分 | 數分鐘 | 5–30 分 | 秒–數分 | 秒–數分 |
| **核心風險** | 假突破 | 趨勢日逆勢 | 主觀結構判斷 | 需要 L2 資料 | 過擬合 / 成本 |

---

## 8. 實務提醒

1. **1 分 K 對成本極敏感**：0.1% 手續費 + 滑價，若目標只有 0.2–0.3%，大部分毛利會被吃掉。
2. **Al Brooks 明言**：多數人無法在 1 分 K 穩定決策；2 分是勉強下限。
3. **ICT/SMC 原始架構在 HTF**：1 分只是 execution layer，獨立用 1 分假突破率偏高。
4. **學術 DRL/LOB 模型**：需要 tick 級資料與基礎設施，非 TradingView 可直接部署。
5. **專業 scalper 風控參考**：單筆風險 0.25–0.5% 權益；日虧上限 1.5–2%；time stop 5–8 分鐘未動則平倉。
6. **本目錄與專案既有策略無對照關係**；若後續要回測，需另立 research topic 登錄 `config/research.yaml`。

---

## 9. 參考連結

### 學術

- [TFF-CL-GRU — MDPI Applied Sciences (2024)](https://www.mdpi.com/2076-3417/14/7/2984)
- [DRL for HFT — arXiv 2101.07107](https://arxiv.org/abs/2101.07107)
- [DDQL LOB Execution — arXiv 2301.08688](https://arxiv.org/abs/2301.08688)
- [HMM-SVM HF Regime — Springer (2025)](https://link.springer.com/article/10.1007/s11009-025-10148-8)
- [JAX-LOB Simulator — arXiv 2308.13289](https://arxiv.org/abs/2308.13289)
- [A Profitable Day Trading Strategy — Concretum / SFI (2024)](https://www.sfi.ch/en/publications/n-24-98-a-profitable-day-trading-strategy-for-the-u.s.-equity-market)
- [Day trading returns across volatility states — Holmberg et al.](https://www.diva-portal.org/smash/get/diva2:732318/FULLTEXT02.pdf)
- [Avellaneda-Stoikov (2008)](https://www.math.nyu.edu/faculty/avellane/HighFrequencyTrading.pdf)

### 作者／教學

- [Toby Crabel — ORB 書籍摘要](https://tradelosstracker.com/library/book/141-day-trading-with-short-term-price-patterns-and-opening/extended)
- [1-Minute ORB — TradingSim / Aziz](https://www.tradingsim.com/blog/1-minute-orb)
- [Al Brooks — Rules for Scalping](https://www.brookstradingcourse.com/trading-strategies/rules-for-scalping/)
- [Ross Cameron — Gap and Go](https://www.warriortrading.com/gap-go/)
- [ICT Silver Bullet Guide](https://www.ictkillzone.com/ict-silver-bullet)
- [ICT Venom Model](https://www.ictkillzone.com/ict-venom-model)
- [ICT IOFED](https://www.ictkillzone.com/ict-iofed)
- [SMC 1-Minute Scalping — GrandAlgo](https://grandalgo.com/blog/1-minute-scalping-strategy-smart-money)

### 工具／Order Flow

- [Bookmap Footprint Docs](https://bookmap.com/knowledgebase/docs/Addons-Footprint)
- [Order Flow Scalping — Bookmap Blog](https://bookmap.com/blog/can-real-time-order-flow-give-you-an-edge-in-scalp-trading)

### 指標模板

- [Four 1-Minute Scalping Strategies — FXOpen](https://fxopen.com/blog/en/1-minute-scalping-trading-strategies-with-examples/)
- [Five Scalping Setups — technical-analysis-pro](https://www.technical-analysis-pro.com/strategies-scalping/)

---

## 10. 後續可細化項目

若需將任一模型推進為可回測規格，可從下列線別擇一細化：

| 代號 | 內容 |
|------|------|
| **A** | Crabel 1-min ORB 完整規格（stretch 計算、NR4 過濾、進出場） |
| **B** | ICT Silver Bullet / Venom 1M 進場 checklist |
| **C** | VWAP + ORB 量化版（Concretum 論文參數） |
| **D** | Order flow 1M footprint 訊號定義 |
| **E** | Bollinger-RSI mean reversion 原始參數集 |

---

*本文件為 research layer 外部方法論蒐集；更新時請同步修訂 Version 欄位。*
