# 低檔回升且外資投信同步買超 · 回測計畫

> **research topic**: `finmind-low-rebound-foreign-it-sync`  
> **spec_fidelity**: `faithful`（進出場規則需補充 · 見 §4）  
> **priority**: P0

---

## 1. 原作者條件（FinMind 策略市集）

**開盤前符合全部條件**（AND）：

| ID | 條件 | 閾值 |
|----|------|------|
| E1 | 股票 · 最近 10 日內至少 1 日外資買超 | > 500 張 |
| E2 | 股票 · 最近 10 日內至少 1 日投信買超 | > 200 張 |
| E3 | 股票 · 連續 5 年現金股利 | ≥ 1 元 |
| E4 | 股票 · 年度股東權益報酬率（ROE） | ≥ 10% |
| E5 | 股票 · 日 K 突破低檔區 20 | KD 低檔穿越（見 §3） |

出場：原作者未在截圖列出明確規則 → 本計畫採 **研究預設**（§4）。

---

## 2. 資料對照（PIT）

| 條件 | 表 / 欄位 | 訊號日 T 取值 |
|------|-----------|---------------|
| E1 | `stock_institutional_daily.foreign_net` | `trade_date ∈ [T-9, T]` 任一筆 `> 500` |
| E2 | `stock_institutional_daily.investment_trust_net` | 同上 `> 200` |
| E3 | `stock_dividend_history` | 連續 5 個 fiscal_year · `cash_dividend ≥ 1` · 以 `ex_cash_date ≤ T` 判定 |
| E4 | `stock_fundamental.roe_ttm` 或最近年度 `stock_financial_history`（metric=`roe` 衍生） | `≥ 10` · 財報 `period_date ≤ T` |
| E5 | `stock_technical_daily` | `kd_cross_above_20 = 1` on `trade_date = T` |

**同步模組**：`sync_stock_market_daily` · `sync_fundamentals` · `sync_stock_technical_daily` · `backfill_market_data.py`。

---

## 3. E5「低檔區 20」操作定義

| 版本 | 定義 | fidelity |
|------|------|----------|
| **A（預設）** | Taiwan-style KD(9,3,3)：`K` 由 <20 上穿 ≥20 · 欄位 `kd_cross_above_20` | `faithful` |
| B（備選） | `kd_k < 25` 且 `kd_k > kd_d` 且前日 `kd_k ≤ kd_d` | `approx` |

實作以 **A** 為準；B 作 sensitivity。

---

## 4. 交易規則（研究預設 · 待 preregister）

| 項目 | 值 | 說明 |
|------|-----|------|
| Universe | `tw100` + `etf_watchlist` | 分軌出報告 |
| 進場 | T+1 **open** | 對齊「開盤前篩選」 |
| 出場（預設） | 持有 **20 交易日** · T+20 close | 原作者未指定；對照其他 FinMind 策略慣例 |
| 出場（備選） | 停損 8% · 停利 15% · 或 20 日到期 | sensitivity run |
| 槽位 | **5 槽** · 等權 | 與 VCP 軌對齊便於比較 |
| 排序 | 訊號日 `three_institution_net` 降序 | tie-break：`return_5d_pct` 升序（低檔回升語意） |

---

## 5. 回測窗口

| window_id | 區間 | 用途 |
|-----------|------|------|
| `w_tw100_full` | 2015-01-01～ | TW100 主窗口 |
| `w_watchlist_730d` | 滾動 730 日 | 與 etf_watchlist 增量同步對齊 |
| `w_oos_2024` | 2024-01-01～ | 樣本外（若 IS 用 2015–2023） |

---

## 6. 產出與驗收

- **Runner（待實作）**：`scripts/run_finmind_low_rebound_backtest.py`
- **Artifacts**：`reports/research/finmind-marketplace/low_rebound_foreign_it_sync_backtest_YYYYMMDD.{json,md}`
- **G2 門檻**：walk-forward 或 `w_oos_2024` interval excess > 0 · `n_trades ≥ 30`

---

## 7. 已知差異

| 項目 | 說明 |
|------|------|
| 出場規則 | 原作者未載明 → 本計畫 20 日持有為 **研究假設** |
| ROE | 以 `roe_ttm` 近似「年度 ROE」；可改最近完整年度 |
| 股利 | 以 `ex_cash_date` 年度聚合；除權息調整未建模 |
