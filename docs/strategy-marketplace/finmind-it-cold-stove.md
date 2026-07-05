# 投信燒冷灶 · 回測計畫

> **research topic**: `finmind-it-cold-stove`  
> **spec_fidelity**: `simplified`（缺投信持股餘額）  
> **priority**: P2

---

## 1. 原作者條件（FinMind 策略市集）

**進場（開盤前）**：

| ID | 方向 | 條件 |
|----|------|------|
| E1 | 多 | 股票 · 投信買超 > 300 張 |
| E2 | 空（篩選） | 股票 · 昨天投信持股 < 2000 張 |

**出場**：

| ID | 類型 | 值 |
|----|------|-----|
| X1 | 停損 | 10% |
| X2 | 停利 | 15% |
| X3 | 持有天數 | 未設定 |

---

## 2. 資料對照

| 條件 | 表 / 欄位 | 狀態 |
|------|-----------|------|
| E1 | `stock_institutional_daily.investment_trust_net` | ✅ `> 300` on T |
| E2 | 投信持股餘額 | ❌ **無 dataset** · `stock_shareholding_daily` 為外資持股 |

---

## 3. 版本矩陣

| 版本 | 進場 | fidelity |
|------|------|----------|
| **v1-simplified** | 僅 E1：`investment_trust_net > 300` on T | `simplified` · **預設** |
| v2-proxy-holdings | 以 `investment_trust_net` 累積近似持股變化 | `approx` · 不建議 |
| v3-faithful | E1 + E2 | `faithful` · **blocked** · 待 FinMind 投信持股表 |

### v1 語意

「燒冷灶」原意為投信在 **低持股** 標的上開始買超；v1 僅驗證 **投信買超動能** 是否具 alpha，**不**宣稱復現完整原作者邏輯。

---

## 4. 交易規則（v1 預設）

| 項目 | 值 |
|------|-----|
| Universe | `tw100` + `etf_watchlist` |
| 進場 | T+1 open · 做多 |
| 出場 | 停損 **-10%** · 停利 **+15%** · 先觸發者 · 上限 **40 交易日** |
| 槽位 | **5 槽** |
| 排序 | `investment_trust_net` 降序 |

停損停利以 **進場價** 為基準 · 日內用 high/low 觸發（需 `stock_daily_bars` OHLC）。

---

## 5. 回測窗口

| window_id | 區間 |
|-----------|------|
| `w_tw100_full` | 2015-01-01～ · 需 institutional 長歷史 backfill |
| `w_recent_2y` | 2024-07～ · 現有 `stock_institutional_daily` 覆蓋 |

**資料備註**：法人表目前約 2024-07 起有資料；長窗口需擴 `sync_stock_market_daily` backfill。

---

## 6. 產出

- **Runner（待實作）**：`scripts/run_finmind_it_cold_stove_backtest.py`
- **Artifacts**：`reports/research/finmind-marketplace/it_cold_stove_backtest_YYYYMMDD.{json,md}`

---

## 7. 解鎖 E2 之路

| 步驟 | 動作 |
|------|------|
| 1 | 調查 FinMind / TEJ 投信持股餘額 dataset |
| 2 | 新增表 `stock_it_shareholding_daily` + sync |
| 3 | 跑 v3 對照 v1 · 量化簡化偏差 |

---

## 8. 已知差異

| 項目 | 說明 |
|------|------|
| E2 缺失 | 核心「冷灶」濾網無法復現 |
| 空方向 | E2 在截圖標「空」· 可能為排除條件而非做空進場 · v1 僅做多 |
| 張數單位 | 千股 = 張（見目錄 README） |
