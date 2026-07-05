# 策略市集（Strategy marketplace）· 回測計畫索引

> **Layer**: research（探索性 · 未採納）  
> **來源**: FinMind 策略市集螢幕截圖（2026-07）  
> **SSOT**: 本目錄各策略頁 · 機器登錄 `config/research.yaml` → `topics.finmind-*`  
> **資料層**: screener backfill · `docs/agent-brief.md` · `src/backfill_market_data.py`

---

## 狀態總覽

| # | 策略 | research topic | 資料就緒度 | 回測優先 |
|---|------|----------------|------------|----------|
| 1 | [低檔回升且外資投信同步買超](finmind-low-rebound-foreign-it-sync.md) | `finmind-low-rebound-foreign-it-sync` | 高（灌庫後） | **P0** |
| 2 | [低市銷率的成長股](finmind-low-ps-growth.md) | `finmind-low-ps-growth` | 高（灌庫後） | **P0** |
| 3 | [指數期貨 · 外資淨多單持續 3 日](finmind-tx-foreign-net-long-3d.md) | `finmind-tx-foreign-net-long-3d` | 中（期貨 OI + 指數 K） | **P1** |
| 4 | [突破月線長紅放量](finmind-ma20-breakout-volume.md) | `finmind-ma20-breakout-volume` | 中（日線近似） | **P2** |
| 5 | [投信燒冷灶](finmind-it-cold-stove.md) | `finmind-it-cold-stove` | 低（缺投信持股） | **P2 簡化版** |

**共通前提**

- **PIT**：訊號日 T 僅用 `date ≤ T` 收盤資料；現股進場預設 **T+1 open**（與 FinMind「開盤前符合」對齊）。
- **Universe 預設**：`tw100`（長歷史）＋ `etf_watchlist`（成分監測）分軌報告；實作時以 `screener_universe.resolve_sync_watchlist` 為準。
- **基準**：個股槽位策略 · `IX0001`（`config/backtest_standard.yaml`）。
- **成本**：`buy_fee_pct 0.1425` · `sell_fee_pct 0.1425` · `sell_tax_pct 0.3`（比較層預設）。
- **法人張數**：FinMind `TaiwanStockInstitutionalInvestors` 欄位為 **千股**；台股慣例 **1 張 = 1 千股**，回測實作時閾值直接對齊「張」。

---

## 尚未納入本目錄（資料缺口）

| 策略類型 | 缺口 |
|----------|------|
| 法說會前主力買超 | 無法說會日曆 · 「主力／關鍵券商」定義與 `stock_branch_daily` 不一致 |
| 代操券商中小型股 | 無「代操」券商分類 · Sponsor 分點僅 Top N |

---

## 產出契約（各策略共通）

```text
config/research.yaml  →  topics.finmind-*
        ↓
（待實作）scripts/run_finmind_*_backtest.py
        ↓
reports/research/finmind-marketplace/{topic_id}_backtest_YYYYMMDD.{json,md}
```

| 欄位 | 說明 |
|------|------|
| `spec_fidelity` | `faithful` · `approx` · `simplified` |
| `universe_id` | `tw100` · `etf_watchlist` |
| `window_primary` | `2015-01-01`～`null`（TW100）· `730d`（watchlist） |
| `metrics` | 組合總報酬% · interval excess vs IX0001 · 勝率 · Sharpe · 最大回撤 |

---

## 延伸閱讀

| 文件 | 用途 |
|------|------|
| [evaluation-contract.md](../evaluation-contract.md) | 回測 JSON 契約 |
| [unified-backtest-standard.md](../unified-backtest-standard.md) | 跨軌比較層 |
| [agent-brief.md](../agent-brief.md) | 任務導航 |
