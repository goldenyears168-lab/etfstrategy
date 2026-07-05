# 低市銷率的成長股 · 回測計畫

> **research topic**: `finmind-low-ps-growth`  
> **spec_fidelity**: `faithful` + `approx`（櫃買 MA · 見 §6）  
> **priority**: P0

---

## 1. 原作者條件（FinMind 策略市集）

**開盤前 · 硬性條件（全部 AND）**：

| ID | 條件 | 閾值 |
|----|------|------|
| H1 | 股票 · 市值營收比（年） | < 5（解讀為 **P/S < 5 倍** · 非 5%） |
| H2 | 股票 · 營收較去年同期成長 | ≥ 50% |
| H3 | 股票 · 連續 2 季營業利益率 | ≥ 5% |
| H4 | 股票 · 股本 | ≥ 10 億 NTD |
| H5 | 股票 · 5 日平均成交值 | ≥ 0.2 億 NTD |

**開盤前 · 市場濾網（至少 1 個 OR）**：

| ID | 條件 |
|----|------|
| M1 | 大盤站在 5 日均線之上 |
| M2 | 櫃買指數站在 5 日均線之上 |

---

## 2. 資料對照（PIT）

| 條件 | 表 / 欄位 | 訊號日 T |
|------|-----------|----------|
| H1 | `stock_market_value_daily.mcap_to_revenue_ttm` | `< 5` · `trade_date = T` |
| H2 | `stock_fundamental.revenue_yoy_pct` | `≥ 50` · 月營收公布日 ≤ T |
| H3 | `stock_financial_history` · metric=`operating_margin_pct` | 最近連續 2 季 `≥ 5` · `period_date ≤ T` |
| H4 | `stock_shareholding_daily.capital_ntd` | `≥ 1e9` |
| H5 | `stock_daily_bars` | `mean(close×volume, 5d) ≥ 2e7` |
| M1 | `daily_bars` · `stock_id=IX0001` | `close > ma5`（本地計算） |
| M2 | 櫃買指數日線 | **缺口** · 見 §6 |

---

## 3. H3 連續兩季判定

1. 取 `period_type='Q'` · `metric='operating_margin_pct'` · `period_date ≤ T` 排序。
2. 最近兩筆皆 `≥ 5` → pass。
3. 若僅有累季 YTD 列 · 退化为最近兩個 **已公布季度** 各有一筆 margin。

---

## 4. 交易規則（研究預設）

| 項目 | 值 |
|------|-----|
| Universe | `tw100`（主）· `etf_watchlist`（對照） |
| 進場 | T+1 open |
| 出場 | 持有 **60 交易日** · close（成長股語意 · 較長持有） |
| 槽位 | **5 槽** · 等權 |
| 排序 | `revenue_yoy_pct` 降序 · tie：`mcap_to_revenue_ttm` 升序 |

---

## 5. 回測窗口

| window_id | 區間 |
|-----------|------|
| `w_tw100_full` | 2015-01-01～ |
| `w_watchlist_730d` | 730 日 |
| `w_regime_stratify` | 依 `IX0001` 站/破 MA5 分層報告 |

---

## 6. 近似版本

| 版本 | M 濾網 | fidelity |
|------|--------|----------|
| **v1-faithful-ix** | 僅 M1（加權 MA5） | `approx` · **預設先跑** |
| v2-dual-index | M1 OR M2 · M2 用 FinMind 指數 `101` 補 K 線 | `faithful` · 需 ingest |
| v3-no-market | 移除 M1/M2 | `simplified` · 純因子檢定 |

**櫃買缺口**：TEJ 基準目前 `IX0001` + `IR0002`；櫃買加權需 `FinMind TaiwanVariousIndicators5Seconds` 或指數代碼 `101` 日線 backfill。

---

## 7. 產出

- **Runner（待實作）**：`scripts/run_finmind_low_ps_growth_backtest.py`
- **Artifacts**：`reports/research/finmind-marketplace/low_ps_growth_backtest_YYYYMMDD.{json,md}`

---

## 8. 已知差異

| 項目 | 說明 |
|------|------|
| P/S 分母 | `mcap_to_revenue_ttm` 為 TTM 營收 · 與「年」標籤可能差一季 |
| 營收 YoY | `stock_fundamental` 為最新月；非財報季末快照 |
| 成交值 | 用收盤×成交量 · 非加權成交金額（FinMind 若有 amount 可升級） |
