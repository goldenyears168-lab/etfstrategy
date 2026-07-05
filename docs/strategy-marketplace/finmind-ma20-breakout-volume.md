# 突破月線長紅放量 · 回測計畫

> **research topic**: `finmind-ma20-breakout-volume`  
> **spec_fidelity**: `approx`（盤中放量以日線 proxy）  
> **priority**: P2

---

## 1. 原作者條件（FinMind 策略市集）

**開盤前符合全部（AND）**：

| ID | 條件 |
|----|------|
| P1 | 股票 · 收盤價突破 20 日均線 |
| P2 | 股票 · 漲幅 ≥ 5% |

**盤中符合全部**：

| ID | 條件 |
|----|------|
| I1 | 股票 · 預估量大於前 5 日均量的 2 倍 |

---

## 2. 資料對照

| 條件 | 表 / 欄位 | 訊號日 T |
|------|-----------|----------|
| P1 | `stock_technical_daily` | `close > ma20` 且前日 `close ≤ ma20`（突破） |
| P2 | `stock_technical_daily.return_1d_pct` | `≥ 5` |
| I1 | **無 1 分 K** | 見 §3 近似 |

K 線來源：`stock_daily_bars` · 技術指標 `sync_stock_technical_daily`。

---

## 3. 版本矩陣

| 版本 | P1–P2 | I1 放量 | 進場時點 | fidelity |
|------|-------|---------|----------|----------|
| **v1-simplified** | T 日收盤確認 | `vol_ratio_5d ≥ 2` on T | T+1 open | `approx` · **預設** |
| v2-intraday | 同左 | 1m 累積量 / 時間外推 | T+1 09:30 後條件觸發 | `faithful` · 需 `stock_kbar_1m` |
| v3-preopen-only | 僅 P1+P2 | 略過 I1 | T+1 open | `simplified` |

### v1 I1 近似說明

- `vol_ratio_5d = volume_T / mean(volume_{T-5..T-1})`
- 以 **全日成交量** 代理「盤中預估量」· 與原作者盤中邏輯有 **系統性偏差**（常低估開盤前訊號日）

### P1 突破定義

- **嚴格**：`close_T > ma20_T` 且 `close_{T-1} ≤ ma20_{T-1}`
- **寬鬆**（sensitivity）：`close_T > ma20_T` 即可

---

## 4. 交易規則（v1 預設）

| 項目 | 值 |
|------|-----|
| Universe | `tw100` + `etf_watchlist` |
| 進場 | T+1 open |
| 出場 | 持有 **10 交易日** · close（短線突破） |
| 槽位 | **5 槽** |
| 排序 | `return_1d_pct` 降序 |

---

## 5. 回測窗口

| window_id | 區間 |
|-----------|------|
| `w_tw100_full` | 2015-01-01～ |
| `w_intraday_ab` | 2024-01-01～ · 僅當 v2 有 1m 覆蓋時 |

---

## 6. 產出

- **Runner（待實作）**：`scripts/run_finmind_ma20_breakout_backtest.py`
- **Artifacts**：`reports/research/finmind-marketplace/ma20_breakout_volume_backtest_YYYYMMDD.{json,md}`

---

## 7. 升級路徑

1. v1 日線 proxy 出 baseline JSON。  
2. 對 2024+ 子樣本跑 v2（`stock_kbar_1m`）· 量化 v1 偏差。  
3. 若 v2 顯著優於 v1 · 將 I1 定義寫入 frozen spec。
