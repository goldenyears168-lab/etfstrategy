# 三日趨勢預測 · 研究計畫（FROZEN）

Research only · 未採納 · 2026-07-23

## 目標

找到 **PIT、可复現** 的日頻特徵／規則，使「未來 3 個交易日方向或期望報酬」明顯優於：

| 基準 | 目前水準 |
|------|----------|
| 擲硬幣方向命中 | ~50% |
| 既有 `trend_3d` 溫度計 | 1d/3d/5d 命中 ≈45–53%（無優勢） |
| naive `sign(r3)` 延續 | 3d 命中 ≈48–52% |

## 凍結協議（所有平行支線共用）

| 鍵 | 值 |
|----|-----|
| 宇宙 | 核心：`2327,8046,3189,2492`；輔助：`6451`（樣本短，分開報） |
| 視窗 | 約 2024-07-21 → DB max（~2y）；另報 IS 前半 / OOS 後半 |
| 訊號時點 | T **收盤**；特徵僅用 `date ≤ T` |
| 標的 | **主**：`fwd_3d = close[T+3]/close[T]-1`；輔：1d/5d、vs IX0001 excess |
| 方向命中 | `sign(signal)==sign(fwd)` 且 `|fwd|≥0.1%` |
| 更重要 | 分桶 **mean fwd_3d / excess**、多空分離（只做多／只避開） |
| 資料 | `data/stocks.db`：`stock_daily_bars` · `daily_bars` IX0001 · `stock_institutional_daily` |
| 成本 | 報 raw；另列 30bps round-trip 敏感性（可選） |
| 禁止 | 偷看 T+1…；把結果寫進 Order／strategy.yaml |

## 成功閘（支線「值得合併」）

任一支線達其一即可進合併池：

1. **Directed 3d ≥ 55%** 且 OOS 半段 ≥54%，或  
2. **temp/訊號多頭桶** mean `fwd_3d` − **空頭桶** ≥ **+1.5pp**（且兩桶 n≥80），或  
3. 只做多規則：相對「每日都做多」 **excess 3d** 提升且 OOS 不翻號。

## 平行支線（多 agent）

| ID | 支線 | 假說 | 負責 |
|----|------|------|------|
| A | Price MR/MOM grid | 短窗報酬延續 vs 反轉可分出可交易邊 | Agent A |
| B | Chip lead-lag | 法人淨額領先價 1–3 日 | Agent B |
| C | RS vs IX0001 | 相對強度／殘差動能預測個股 3d | Agent C |
| D | Classic TA | RSI/MACD/均線/布林 日頻對 3d 有邊 | Agent D |
| E | Vol/gap/ATR | 波動壓縮後突破或高潮後反轉 | Agent E |

## 合併（主會話）

支線回報後：挑過閘特徵 → 小Ablation → 更新 `trend_3d` 或另立 `trend3d_v2`（仍 research）。

## 產出路徑

`reports/research/intraday_direction_thermometer/trend3d_hunt_20260723/`
