# Live TA 欄位優化：1-2分動能 + 30分鐘偏向（2026-07-28）

## 0. 背景

網站 Live TA 面板（`ops.live_ta`，`src/ops_live_ta.py`）有兩個一直存在、但從未被嚴謹回測驗證過的欄位：

1. **1-2分動能「偏強/偏弱」** — 舊邏輯：`mom_2bar_pct`（或 `mom_1bar_pct`）以固定 ±0.35% 門檻判斷「偏強/偏弱」，語意是**延續**（動能往同方向繼續）。
2. **30分鐘偏向 `ta_30m_bias`** — 舊邏輯：`ret_pct`（相對 bar open）與 `vs_vwap_pct`（相對現價 VWAP）同向時判「偏多/偏空」，同樣是延續語意。

2026-07-28 稍早的研究（`H3_RESIDUAL_SHORT_MOM.md`、`TA_30M_BIAS_EVAL.md`）已確認兩者皆無統計意義：

| 欄位 | 舊公式 OOS 命中率 | 結論 |
|---|---|---|
| 1-2分動能延續 | 35–39% | 遠低於 50%（反向訊號更強） |
| 30分鐘 confluence 延續 | 45.23% | 統計上等同丟銅板（55%以下皆算無edge） |

用戶指示：「請將這些沒有幫助欄位，製作更進階的研究，優化他們，直到有幫助。如果還是沒有幫助，請移除這個欄位」。本輪（多 agent workflow + 文獻回顧）針對兩個欄位分別做了最後一輪優化嘗試，並將結果落地到 `src/ops_live_ta.py`。

## 1. 1-2分動能 → 改為「振幅過濾＋反向（fade）」

### 1.1 文獻依據

Roll (1984) bid-ask bounce model：極短窗口（1-5分）價格常態性由買賣價差彈跳主導，動能延續在這個尺度上通常是雜訊而非資訊，**反向**（fade）在微結構層次較合理。這與 H3 報告觀察到的「fade short-mom ≈ 58–65%」方向一致。

### 1.2 本輪新研究：振幅過濾（`scripts/research/run_short_mom_fade_amplitude_filter.py`）

假說：反向訊號的真正 edge 集中在「異常放大的動能」，而非任何非零動能都值得反向。用「trailing 10 交易日 |1分bar動能| 分布的前 5%」當作觸發門檻（PIT-safe：門檻只用嚴格早於當日的歷史資料，當日資料在收盤後才併入下一次門檻計算）。

**Champion**：`raw_mom_1__pct_top5_D10`（1分bar動能，fade 方向，僅在 |動能| ≥ 近10日前5%極端值時觸發，horizon=5分鐘）

| | IS (≤2025-09-30) | OOS |
|---|---|---|
| n_directed | 17,886 | 10,649 |
| 方向命中率 | 77.42% | **72.97%** |
| always_long baseline | 48.02% | 48.35% |
| gates | — | 全通過（`oos_n_ge`/`oos_hit_ge`/`name_floor_pass`/`max_name_share_ok`） |

7檔個股（2303/2327/2330/2454/3189/6451/8046）OOS 個股命中率 64.8%–88.9%，無單一名稱主導（max_name_share=21.9%）。

**廣義化檢查**（`fade_mom_1_2m_universe_generalization.json`，11檔含 6505/6147/3653/2492 等真正在網站上出現過的名稱，同期間但無振幅過濾）：`fade_mom_1` h=5 OOS 62.13%（n=113,130，gates 全過）。→ 即使不加振幅過濾，fade 方向本身就在更廣的股票集上有穩定 edge；振幅過濾把 edge 集中到更高但樣本較窄的 7 檔上。

### 1.3 落地實作

`src/ops_live_ta.py`：

- 新函式 `compute_mom1_fade_threshold(conn, stock_id, today)`：從本地 `stock_kbar_1m` 撈出「嚴格早於今天」的最近 10 個交易日，算出 |1分bar動能| 分布的前5%門檻（PIT-safe，不看今天）。**已知限制**：`stock_kbar_1m` 目前並非每日自動同步整個動態 universe（見第3節），所以門檻的新鮮度（`stale_days`）逐檔不同，函式誠實回報，不假裝有資料。
- `_continuous_action_note()` 整個改寫：拿掉舊的固定 ±0.35% 延續判斷；新邏輯只在 `|mom_1bar_pct| ≥ 該股門檻` 時才發出方向性字樣：
  - 動能為正 → `急漲(觀察拉回)`
  - 動能為負 → `急跌(觀察反彈)`
  - 門檻不可用（本地歷史不足5天）或動能未達門檻 → 維持 `觀望`（不硬猜）
- `anchors` 新增 `mom1_fade_rule`/`mom1_fade_threshold_pct`/`mom1_fade_n_days`/`mom1_fade_stale_days`/`mom1_fade_oos_hit_pct`(=72.97)/`mom1_fade_oos_n`(=10649)/`mom1_fade_disclaimer_zh`，供網站顯示依據與新鮮度。

## 2. 30分鐘偏向 `ta_30m_bias` → 移除弱公式，統一到既有 champion 門檻

### 2.1 本輪新嘗試（皆未通過）

| 嘗試 | 結果 | 結論 |
|---|---|---|
| VWAP z-score fade（`run_vwap_zscore_fade_backtest.py`） | OOS 52.95%（champion variant `z2.0_expanding_full_noc_not`，n=7,805） | 遠低於 55% gate，拒絕 |
| ORB fail-fade（`run_orb_fail_fade_backtest.py`） | 未通過穩定性 gates | 拒絕 |
| 7檔更廣泛/更新鮮組合的 `fade_near_ext` | IS/OOS 佳（77.91%，n=1,399，gates 全過）但… | 用**今天實際網站顯示的6檔**（動態 universe）重測同一公式：**OOS 只有 61.2%，未過 gates** |

最後一項是本輪最重要的發現：**「哪一組股票」比「用哪個公式」對這個欄位的可靠度影響更大**。網站的 Live TA universe 是「目前持股 ∪ 網站關注清單」，逐日變動，且大多不等於任何一次研究驗證用的固定股票池。任何只做「换公式」的優化，只要 universe 仍然是任意動態集合，就無法穩定超過 70% gate。

### 2.2 落地決策

不採用本輪任何一個新公式（全部未過 gate 或未廣義化）。改為：**移除**舊的、已證實無 edge（45.23% OOS）的 `ret_pct`/`vs_vwap_pct` 固定 eps confluence 判斷本身，`ta_30m_bias` 預設回到 `中性`（不下判斷），只有在既有 research champion badge `fade30_observe`（規則 `fade_idx_or_inside` = `fade_near_ext` ∧ 0050 開盤區間未破，OOS≈71%，已經是本模組原本就有、有 PIT 大盤閘門保護的判斷）觸發時，才把 `ta_30m_bias` 設成對應方向（`偏多`/`偏空`）。

理由：與其保留一個「看起來永遠有答案、但答案跟丟銅板差不多」的欄位，不如讓欄位「大部分時間誠實地說不知道，少數時間給一個真的有研究依據的方向」——這正是用戶「優化到有幫助，否則移除」指示下，對「移除」與「優化」兩個選項的合理折衷：移除的是無 edge 的公式本身，保留（且成為唯一來源）的是有 edge 但保守（很少觸發）的公式。

`ta_30m_ret_pct` / `ta_30m_vs_open_pct` / `ta_30m_vs_vwap_pct` 三個描述性數字（非方向判斷）維持不變 — 這些只是事實陳述，沒有「有幫助/沒幫助」的問題。

## 3. 系統性盲點：`stock_kbar_1m` 沒有涵蓋動態 universe 的每日同步

檢查發現本地 `stock_kbar_1m`（1分K historical 資料表）**不是**一個對當前 Live TA 動態 universe 保持每日新鮮的表；它是研究用的手動/半自動回填表。抽查 2026-07-28 當天：

| 股票 | 最新 trade_date | 落後天數 |
|---|---|---|
| 2327 | 2026-07-27 | 1 |
| 2492 | 2026-07-27（僅15根bar，資料極稀疏） | 1（但樣本不足，不觸發） |
| 6451 | 2026-07-22 | 6 |
| 8046 / 3189 / 2330 / 6505 | 2026-07-16 | 12 |
| 2303 / 2454 | 2026-07-09 | 19 |
| 3653 | 2026-07-08 | 20 |

`compute_mom1_fade_threshold()` 的設計已經把這個落差當一等公民處理（`stale_days` 誠實回報，樣本不足時 `threshold=None` 不觸發），所以**不會**因為資料舊而假裝有訊號。但實務上，這代表今天很多持股的「急漲/急跌」欄位會長期停在「觀望」（因為門檻算不出來），直到有一個每日同步 job 把 `stock_kbar_1m` 補齊到動態 universe。

**這需要一個新的排程任務**（例如仿造 `scripts/tools/backfill_kbar_5m_from_1m.py` 或既有 FinMind 1分K 回補流程，每日收盤後對「昨日+今日新進 universe 成分」跑增量回補），屬於 mini 上的部署變更，本次未執行（依規範：非經明確同意不變更 mini）。

## 4. 尚未解決 / 後續建議

1. **部署到 mini**：本次修改僅落在本機 repo（`src/ops_live_ta.py`、`tests/test_ops_live_ta.py`）。網站 / Supabase `ops.live_ta` 顯示的內容要更新，需要把這個 branch 部署到 mac-mini 的 live poll 服務（`run_live_ta_poll` 排程），這是一個獨立的部署步驟，未經你同意不會執行。
2. **`stock_kbar_1m` 每日同步 job**：如上節，若要讓「急漲/急跌」欄位對整個動態 universe 都有效，需要新增排程；目前只有本身資料較新的少數持股（如 2327）能受益。
3. **30分鐘欄位的長期解法**：需要固定研究 universe 與 Live TA universe 的落差；三個可能方向——(a) 只對「已驗證股票池」啟用該欄位、其餘顯示「未驗證」；(b) 擴大驗證池覆蓋目前所有常見持股/關注清單；(c) 接受目前保守（大部分時間中性）的設計。本次採用(c)的保守版本作為立即修正，(a)/(b) 留待下一輪。

## 5. 驗證

`tests/test_ops_live_ta.py`：31 個測試全過，新增/修改：

- `test_mom1_fade_threshold_and_action` — 驗證 `compute_mom1_fade_threshold` 從本地 kbar 正確算出 PIT 門檻、`stale_days`，以及觸發時 `action="急漲(觀察拉回)"`。
- `test_build_continuous_vs_disposition` — 更新斷言：無 DB 連線時舊 `mom_2bar_pct` 不再觸發已被拒絕的「偏強」判斷，改為誠實回到 `觀望`。
- `test_ta_30m_ready_after_first_half_hour` — 更新斷言：無 `bench_5m` 時 `ta_30m_bias` 回到 `中性`，而非舊的、無 edge 的 confluence 判斷。
