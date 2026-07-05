# 【指數期貨】外資淨多單持續 3 日 · 回測計畫

> **research topic**: `finmind-tx-foreign-net-long-3d`  
> **spec_fidelity**: `faithful`（進場）· `approx`（出場 MA · 見 §5）  
> **priority**: P1

---

## 1. 原作者條件（FinMind 策略市集）

**進場（開盤前 · 多）**：

| ID | 條件 |
|----|------|
| E1 | 期貨 · 連續 3 日外資淨多單 |

**出場（開盤前 · 空）**：

| ID | 條件 |
|----|------|
| X1 | 執行商品收盤價跌破 5 日均線 |

停損 / 停利 / 持有天數：截圖為 **未設定**。

---

## 2. 資料對照（PIT）

| 條件 | 表 / 欄位 | 說明 |
|------|-----------|------|
| E1 | `futures_institutional_daily` | `futures_id='TX'` · `inst_name` 對應外資 · `net_oi_vol > 0` 連續 3 交易日 |
| X1 | 期貨或指數日 K | `close < ma5` |

**同步**：`sync_futures_institutional_daily.py` · `futures_institutional_daily`。

### E1 判定細節

1. 對每個 `trade_date` T，取外資列（FinMind `Foreign_Investor`）之 `net_oi_vol`。
2. 要求 `T-2, T-1, T` 三日皆 `net_oi_vol > 0`（淨多單）。
3. 訊號在 T 收盤確認 · 進場 T+1（期貨開盤或現股 proxy · 見 §4）。

---

## 3. 標的與方向

| 版本 | 執行標的 | 說明 |
|------|----------|------|
| **A（預設）** | 台指期 TX · 多單 | 忠於「期貨」語意 · 需 TX 日線 + 保證金模型 |
| B | `0050` 或 `IX0001` 指數 proxy | 無期貨 tick 時 · **現股多單** 近似方向性 |
| C | 放空 TX（出場 X1 觸發） | 原作者出場標「空」→ 實為 **平多 + 可選反手空** · 預設 **僅平多** |

本計畫 **Phase 1** 用 **B（0050.TW 或加權指數超額）** 驗證訊號品質 · **Phase 2** 接 TX 期貨日線。

---

## 4. 交易規則（研究預設）

| 項目 | Phase 1（proxy） | Phase 2（TX） |
|------|------------------|---------------|
| 進場 | T+1 open · 做多 | T+1 open · 多單 |
| 出場 X1 | `IX0001` close < MA5 → T+1 open 平倉 | TX close < MA5 → 平倉 |
| 持有上限 | 60 交易日 | 同左 |
| Notional | `comparison_notional_ntd: 100000` | 期貨保證金另建模 |

---

## 5. 出場 MA 近似

| 版本 | 價格序列 | fidelity |
|------|----------|----------|
| **v1-ix0001** | `daily_bars` · `IX0001` · 自算 MA5 | `approx` · **預設** |
| v2-tx-futures | TX 近月連續或 FinMind 期貨日線 | `faithful` · 待 ingest |
| v3-0050 | `0050` ETF 日線 MA5 | `approx` · 可交易 proxy |

---

## 6. 回測窗口

| window_id | 區間 |
|-----------|------|
| `w_futures_full` | 2015-01-01～（與 futures OI backfill 對齊） |
| `w_oos_recent` | 2024-01-01～ |

---

## 7. 產出

- **Runner（待實作）**：`scripts/run_finmind_tx_foreign_net_long_backtest.py`
- **Artifacts**：`reports/research/finmind-marketplace/tx_foreign_net_long_3d_backtest_YYYYMMDD.{json,md}`

---

## 8. 已知差異

| 項目 | 說明 |
|------|------|
| 淨多單定義 | 使用 `net_oi_vol`（未平倉淨額）· 非「買超張數」 |
| 出場「空」 | 截圖為條件出場 · 本計畫預設 **平多** 而非開空 |
| 指數 vs 期貨 | Phase 1 用 IX0001 近似 TX 價格路徑 |
