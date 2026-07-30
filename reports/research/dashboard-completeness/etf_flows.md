# ETF 申贖 / 受益人數 / 折溢價 維度（ETF Flows & Sentiment）研究

維度：**ETF 申贖(創建/贖回=受益權單位數變動)、受益人數(集保持有人數)、折溢價(市價 vs NAV)**
狀態：**真資料已跑（S1 flow + S3 holders）** — FinMind `TaiwanStockHoldingSharesPer` 週頻回填 2018→2026、8 檔主要 ETF；**S2 折溢價＝待補（已窮盡免費歷史 NAV 源、確認無可得，見 §7 gap-fill 2026-07-30）**
**Verdict：非真 alpha（無穩健預測力）+ S2 待補。** 6 個設計變體(受益人數/SHOUT申贖/人均持有 × CORE4/全宇宙)在大盤層 IS IC 皆 ≈0（|IC|≤0.012）；唯一 OOS 正值(hg_all +0.83) permutation p=0.077 未過、且與 champion **正相關+0.37**（非預期的反向鏡像）。
日期：2026-07-30　·　對照 champion：外資台指期 positioning `fut_foreign_oi_z60>0`（OOS Sharpe **+1.116**）
腳本：`scripts/research/dashboard/etf_flows_study.py`（研究）· `etf_flows_fetch.py`（抓資料）· 資料 `data/research/dashboard/etf_flows_data.parquet`

---

## 1. 這維度是什麼、專業為何看它

申贖、受益人數、折溢價是**同一個機制（ETF 一級/二級市場套利）的三個觀測面**，共同用途：量化「散戶 / 非基本面需求（non-fundamental demand）」的擁擠與反轉壓力。

- **申贖（flow）**——ETF 創建/贖回造成的在外受益權單位數（SHOUT）變動。申購=單位增加（正 flow）、贖回=單位減少（負 flow）。是三者中學術最扎實的一格：flow 是非基本面需求 shock 的直接讀數。
- **折溢價（premium/discount to NAV）**——市價相對淨值的偏離，短線情緒溫度計與套利壓力表；單一熱門 ETF 溢價擴大 = froth 前兆。
- **受益人數（beneficiary count）**——集保登記的持有人數，散戶參與度/擁擠度的慢變數；創高＋人均持有下降 = 散戶擁擠頂部前兆。

**相對本專案 champion（外資期貨 positioning = 聰明錢領先）的定位**：這三格是 champion 的**鏡像**——量測「笨錢/情緒」的擁擠。最佳用途是**反向情緒 overlay**（對多頭訊號打折），不是方向性 alpha。

### 學術 / GitHub 依據（本維度調研引用）

- **Brown, Davies, Ringgenberg (2021), _Review of Finance_ 25(4)（SSRN 2872414）** — 核心操作化：`flow = ΔSHOUT/SHOUT` 是非基本面需求 shock 訊號；short-high-flow / long-low-flow 月超額 **1.1–2.0%**，1–6 個月 **reversal（反轉）**。ETF 與成分股共享基本面，故兩者價差（折溢價）= 至少一方被情緒污染的證據。**台股須注意 N 太小，不適合直接搬橫斷面 long/short。**
- **Xu (2022), _Financial Management_** — ETF flow 的資訊性是**條件式**的，與折溢價交互：**溢價日的申購 / 折價日的贖回**才有預測力 → 支持把折溢價當 flow 的閘門（分層 L1 觸發 L2）。
- **QuantPedia（Brown-Davies 日頻綜述）** — 可交易日頻版：僅在折溢價與 flow 同號時進場，淨成本後年化 **14.02%**、Sharpe **0.88** — 給了明確的正規化與時間窗參數。
- **中央財經大學學報 2023(9)** — 折溢價=噪音 shock + 反應不足的錯價，修正呈負向報酬預測、持續約 **4 週** → 折溢價反轉窗口的獨立實證錨點。
- **GitHub 生態** — `FinMind/FinMind` 是資料脊椎（`TaiwanStockHoldingSharesPer` 含 ETF、週頻、people 欄可加總受益人數；但無 ETF NAV/折溢價/單位數 dataset，已查證）；`lvxhnat/pyetfdb-scraper`（美股 SHOUT/flow 處理邏輯可借鑑）。**GitHub 上查無台股 ETF 情緒因子的 OOS 驗證實作**——與 chip-macro 結論一致：此白地無人做過誠實驗證。

---

## 2. 訊號精確定義、正規化、方向

| # | 訊號 | 公式 | 正規化 | 方向先驗 |
|---|------|------|--------|----------|
| **S1** | 申贖 flow `etf_flow` | `(SHOUT_t − SHOUT_{t-1}) / SHOUT_{t-1}` | 對自身 20/60 日 z-score 去趨勢 | **領先, CONTRARIAN（反向）**：高申購→後續回吐；1–6 月 reversal |
| **S2** | 折溢價 `prem` | `(Price_t − NAV_t) / NAV_t` | 除以自身波動得 prem-z，或看是否超 ±1%（台灣正常帶） | **前兆/超短線, mean-revert**：高溢價→短空，折價→短多，1–5 日 |
| **S3** | 受益人數 `holders` / 人均持有 | `Δholders%`（週）、或 52 週 z-score、或人均 = `SHOUT/holders` | 週頻 4–8 週動能 / percentile | **落後→極端處轉前兆**：創高＋人均↓ = 散戶擁擠頂 |

**聚合到大盤（TAIEX）**：把全市場（或高股息類）ETF 的 net flow / 受益人數增速加總成一條「散戶擁擠指數」，當 L0 regime 的反向情緒 overlay：極端擁擠 → 對 champion 多頭訊號打折。

**閘門邏輯（Xu 2022）**：S1 的預測力「只在有折溢價時成立」——**溢價日的申購 / 折價日的贖回**才交易。這正是分層精神：折溢價=L1 觸發、flow=L2 定調。

---

## 3. 資料源（真資料已接：S1/S3；S2 待補）

### 已抓真資料（本研究採用）

**關鍵發現**：FinMind `TaiwanStockHoldingSharesPer` 的 `total` 列**同時**提供 `people`＝總受益人數（S3）與 `unit`＝總在外受益權單位數＝**SHOUT（S1 flow 的分子/分母）**。故 **S1 申贖 flow 與 S3 受益人數皆從此單一週頻 dataset 建成，無需 SITCA/投信爬蟲**（scaffold 原以為 S1 需自建爬蟲，實測可省）。

| 項目 | 內容 |
|------|------|
| dataset | FinMind `TaiwanStockHoldingSharesPer`（集保股權分散，週五結算） |
| 宇宙 | 8 檔主要 ETF：市值型 **0050 / 006208**；高股息 **0056 / 00878 / 00713 / 00919 / 00929 / 00940** |
| 期間 | 2018-01-05 → 2026-07-24（週頻）。0050/006208/0056/00713 全區間；00878(2020-07 起)、00919(2022-10)、00929(2023-06)、00940(2024-03) 依上市日 |
| 呼叫成本 | 每檔 1 次全區間呼叫，共 8 次（sleep 0.3s），配額安全 |
| 落地 | `data/research/dashboard/etf_flows_data.parquet`（2,548 列，欄：date/etf_code/holders/shout） |
| 對齊 | 週五結算 +4 日 publish（保守避前視）→ backward `merge_asof` 到 panel 交易日 |

**S2 折溢價 待補**：FinMind **無** ETF NAV / 折溢價 dataset（實測 `TaiwanStockETFReport`/`TaiwanETFNAV`/`TaiwanStockETFNetAssetValue` 皆回 HTTP 422 enum 拒絕）。市價分子有（`TaiwanStockPrice`/本地 `stock_daily_bars`），但缺官方每日淨值分母，無法乾淨計算。需接 TPEX `info.tpex.org.tw/ETF/` 或投信官網 PremiumDiscount 頁，本輪未做 → verdict 標 **待補**。

### 本地已有（未採用，資料太窄）

| 資產 | 內容 | 為何不足 |
|------|------|----------|
| `stock_daily_bars` | 僅 **0050 / 0056** 兩檔 ETF 日 K（各 278/271 列，**2025-05-26→2026-07-21**） | 只 2 檔、窗僅 ~14 月；折溢價的分子有，但缺分母 |
| `etf_holdings_meta` | **8 檔主動式 ETF** 的 nav + holding_count（nav 非空僅 122 列，2025-05→2026-07） | `nav` 是「持股加總淨值」(source=ezmoney/kgifund) **非官方每日淨值**；`holding_count`=持股**檔數**非受益人數；且與上面兩檔價格**不重疊** |
| `etf_daily_signal_snapshot` | 6 檔 ETF 三大法人淨買超（186 列, 2026-05→07） | 僅輔助，非 flow/holders/prem |
| `panel.parquet` | 大盤層（TAIEX + 三大法人 + 期貨 OI + 融資）1986 列 2018→2026 | 無任何 ETF flow/受益人數/折溢價欄 |

**判定：`implementable_now = FALSE`。** 三訊號本地都缺可靠面板。

### 需接新資料源（三訊號皆需 backfill）

| 訊號 | 首選來源 | dataset / endpoint | 頻率 | 備援 |
|------|----------|--------------------|------|------|
| **S3 受益人數** | FinMind `TaiwanStockHoldingSharesPer`（含 ETF 代號，各分級 people 加總=總受益人數） | `GET /api/v4/data?dataset=TaiwanStockHoldingSharesPer&data_id=<etf>` | 週（週五結算） | 集保 TDCC opendata data.gov.tw **dataset 11452** / SITCA `etf_beneficial.aspx` |
| **S1 申贖/SHOUT** | 需自建爬蟲（FinMind 無 dataset — `TaiwanStockShareholding` 的 NumberOfSharesIssued 針對個股非 ETF 單位） | SITCA `sitca.org.tw` 每日 ETF 規模/單位數；或 TWSE `rwd/zh/ETF/etfInout`（每日申購買回清單含發行單位數） | 日 | 各投信官網每日申購買回申報 |
| **S2 折溢價/官方 NAV** | TPEX ETF 專區（FinMind 無 NAV dataset，已查證） | `info.tpex.org.tw/ETF/`；TWSE ETF 訊息中心 / 投信 PremiumDiscount 頁 | 日（IOPV 盤中每 15 秒） | 玩股網 `/stock/etf/{code}/discount-premium` |

**建議 scaffold 順序**：先用 **S3（FinMind `TaiwanStockHoldingSharesPer`）** 建「ETF 受益人數＋人均持有」週頻面板（最低成本、可回溯 2010），再補 **S1（SITCA 單位數）** 與 **S2（官方 NAV）**，三者對齊成 `etf_sentiment_panel`，套本檔 `eval_signal` 驗證。腳本已提供 `fetch_beneficiary_count()`（S3 可跑，需 `FINMIND_TOKEN`）與 S1/S2 的 `NotImplementedError` scaffold（含來源清單）。

---

## 4. 研究設計（專案標準，證偽優先）

沿用 chip-macro / options-micro 的紀律，**先假設無效**：

1. **forward return**：`fwd(t) = ix_close[t+1]/ix_open[t+1] − 1`（訊號盤後已知，最早可交易=次開盤）。S3 週頻訊號前推到「實際公布日」對齊後，fwd 從下一可交易日起算。成本 4 bps/換手。
2. **方向固定**：由 **IN-SAMPLE IC 符號**決定（不偷看 OOS）；反指標訊號（S1/S3 極端）direction 天生為負。
3. **IS/OOS 70/30 時序切分**（panel 對應 cut @ 2024-02-15）。
4. **permutation**：vs 同曝險隨機擇時（`perm_p`，n=2000）。
5. **Deflated-Sharpe**：印試驗次數供 DSR 套用；單一市場序列 + 少數訊號，OOS 很薄，**任何正值只當弱先驗**（champion 自己 DSR 都僅 borderline 0.869 未過 0.95）。
6. **regime-conditioning**：另報 bull（`ix_close>MA200` 且 MA200 上彎）限定 OOS——擁擠反轉 edge 集中在多頭末端，空頭散戶已離場即失效。
7. **共線防呆（最高危）**：受益人數/申購幾乎必跟漲，是動能偽裝。採信前必先對價格動能（過去 20/60 日報酬）做偏相關/殘差化（`partial_corr`），證明 flow 殘差仍有增量 IC，否則就是重新發明 TSMOM。

### 真資料結果（已實跑 — S1 flow + S3 holders）

大盤散戶擁擠指數：把 8 檔 ETF 的週頻受益人數增率 / 在外單位(SHOUT)增率 z26 去趨勢，聚合成市場層 crowding 訊號，餵進 `eval_signal` 全套證偽管線。方向皆由 IS IC 符號決定（CONTRARIAN 先驗）。

```
panel 2018-06-01..2026-07-29  n=1986  IS/OOS cut @ 2024-02-15
B&H  OOS Sharpe = +0.394        champion OOS = +1.116 (fut_foreign_oi_z60>0)
週訊號 2018-01-05..2026-07-24，集保週五+4d publish，backward merge_asof（無前視）

signal        dir  IC_IS  OOS_Sh  bull  permP   pcMom  vsChmp  combo  expo
hg_core        +1  0.000  -0.168  0.00  0.777   0.012   0.433   0.47  0.37   受益人數增率(CORE4固定成分)
flow_core      +1  0.010  -0.007  0.18  0.762  0.0087   0.511   0.53  0.48   SHOUT申贖(CORE4)
hg_all         -1 -0.012  +0.834  0.97  0.077 -0.0005   0.372   1.17  0.60   受益人數增率(全宇宙中位)
flow_all       +1  0.002  -0.101  0.15  0.732 -0.0038   0.449   0.50  0.41   SHOUT申贖(全宇宙中位)
pcap_core      +1  0.002  -0.039  0.10  0.663 -0.003    0.451   0.55  0.45   人均持有變化(CORE4)
pcap_all       +1  0.000  +0.079  0.37  0.575 -0.007    0.419   0.64  0.48   人均持有變化(全宇宙)
```
（dir=IS IC 符號決定的部位方向；bull=多頭 regime 限定 OOS Sharpe；permP=OOS 同曝險 permutation p；pcMom=控制大盤 20 日動能後偏相關；vsChmp=與 champion 報酬相關；combo=與 champion 50/50 混合 OOS Sharpe；expo=曝險比例。）

**怎麼讀（證偽優先）**：

1. **IS IC 全部 ≈ 0**（|IC| ≤ 0.012，含最乾淨的 CORE4 固定成分）→ 大盤層 ETF 擁擠指數對 TAIEX 次日報酬**幾乎無樣本內資訊**。方向先驗（contrarian 應為負）也只有 hg_all 一個變體在 IS 呈負，其餘皆微正（pro-cyclical），**符號不一致**＝無穩定結構。
2. **唯一 OOS 正值 hg_all（+0.834）不可信**：(a) IS IC=−0.012 近噪音；(b) permutation p=0.077 **未過 0.05**；(c) 曝險 60%、bull-only 0.97 → edge 幾乎全靠多頭期間，(d) **與 champion 正相關 +0.37**——是「跟著 champion 同向」而非設計預期的**反向鏡像**（負相關）。它更像 2024–26 多頭 beta 的殘影，不是獨立的散戶反向 alpha。
3. **申贖 flow（S1）完全無訊號**：flow_core/flow_all OOS −0.007 / −0.101，perm p 0.73–0.76。台股 ETF 週頻淨申贖在大盤層不預測方向。
4. **動能偽裝已排除（好消息）**：所有 pcMom ≈ 0（|.| ≤ 0.012）→ 這些擁擠訊號**不是**重新發明 TSMOM（最高危陷阱未中）；但它們也不含增量方向資訊——是「乾淨的雜訊」而非「被動能污染的假訊號」。
5. **DSR**：n_trials=6 變體、OOS 樣本薄（~590 日），最佳 hg_all perm p=0.077 → Deflated-Sharpe 遠不會過。**無任何變體構成可採信 edge。**

---

## 5. lead_lag 定位 · 分層 · 與 champion 的搭配

| 訊號 | lead/lag | 分層 | 與 champion（外資期貨 positioning）怎麼搭 |
|------|----------|------|-------------------------------------------|
| **S1 申贖 flow** | **領先，但 CONTRARIAN（反向）** | **L2（籌碼核心的對立腳）** | champion 是聰明錢**順著做**、flow 是非基本面需求**反著做**，符號相反、皆「領先」→ 放同一 regime 引擎的**對立兩端**（一正一反互補） |
| **S2 折溢價** | **領先/前兆（超短線反向）** | **L1（價量→L1 觸發）** | 當 S1 的**閘門**（溢價日申購才算數）；大盤層多為同步/噪音 |
| **S3 受益人數** | **落後→極端處轉前兆** | **L0（regime 反向情緒濾網）** | 慢速擁擠濾網：受益人數創高＋人均持有下降時，**對 champion 多頭訊號打折**（froth 減碼） |

**整體（事前設計）**：三格設計為 champion 的鏡像，量測笨錢擁擠，角色是**過濾/前兆**而非獨立方向 alpha。

**實測修正**：大盤層聚合後，S1(申贖)與 S3(受益人數/人均持有)**皆不具穩健預測力**，且唯一勉強的 OOS 正值與 champion **正相關**（未呈鏡像）→ 事前的「反向情緒 overlay」假說在**大盤時序層未獲支持**。可能原因：(a) 台股純可交易 ETF 佔大盤市值/成交比重仍低，散戶 ETF 擁擠是**個股/類股**現象（高股息成分股），聚合到 TAIEX 被稀釋；(b) 週頻 + publish lag 讓短線 froth 反轉窗口(1–5 日)被磨平。**若要救活，方向是「個股/高息類股橫斷面」而非大盤擇時**——但受台股 ETF N 太小限制（980T/adopted-44 老毛病），橫斷面統計力弱。

---

## 5b. Verdict · 覆蓋範圍 · 分層歸屬

**Verdict：非真 alpha（S1 flow + S3 holders 無穩健 edge）＋ S2 折溢價 待補。**

- **不是真 alpha**：6 變體 IS IC≈0、OOS 唯一正值 perm p=0.077 未過、與 champion 正相關非鏡像、DSR 遠不過。
- **不是動能偽裝**：pcMom≈0，最高危的 TSMOM-重造陷阱已排除——訊號是「乾淨雜訊」，非污染假訊號。
- **不是確認/前兆/反向**：大盤時序層無方向資訊，反向鏡像假說未獲支持。
- **S2 待補**：FinMind 無 NAV dataset（422 實測），折溢價本輪未跑。

**落層：L0–L3 哪層？** 事前設計把 S1→L2(籌碼對立腳)、S2→L1(觸發閘門)、S3→L0(regime 濾網)。**實測後：三者在大盤層皆不成立為可用因子**，暫不進任何 L0–L3 生產層；`etf_flows_data.parquet` 保留為**可重用資料資產**（受益人數/SHOUT 週頻面板 2018→2026），供日後個股/類股橫斷面研究或做敘事型擁擠觀察（非交易訊號）。

**與 champion 怎麼搭**：實測 combo50（與 `fut_foreign_oi_z60` 50/50 混合）最佳僅 hg_all 1.17（vs champion 單獨 1.116，+0.05 邊際、來自不可信的 hg_all）；其餘變體 combo 0.47–0.64 **拖累** champion。→ **不建議與 champion 混用**。champion 仍是獨走冠軍。

**已抓範圍 vs 完整宇宙（誠實）**：
- ETF 宇宙：8 檔（市值 2 + 高息 6），涵蓋台股 ETF 受益人數/規模的絕大多數（0050/0056/00878 三檔即佔散戶受益人數大宗），但**非全部**（未含 00891/00692/00850/債券 ETF 等數十檔）。
- 訊號：S1(申贖 flow via SHOUT) ✅ 跑、S3(受益人數 + 人均持有) ✅ 跑、S2(折溢價) ❌ 待補。
- 時間：2018→2026 週頻（新兵 ETF 依上市日較短）。OOS=2024-02→2026-07 約 590 交易日（薄）。
- 未做：個股/高息類股橫斷面（下一步候選）、盤中 IOPV 折溢價、公司行動(除息/成分調整)剔除。

---

## 6. 已知陷阱與規避

1. **與價格動能共線（最高危）**：受益人數、申購幾乎必然跟漲。→ 回測前必做 `partial_corr` 對 20/60 日報酬殘差化，殘差仍有增量 IC 才採信。
2. **前視/資料延遲**：受益人數週五結算但數日後才公布（集保/SITCA）；折溢價 NAV 有時區/收盤時點錯位（含海外成分尤甚），盤中 IOPV ≠ 收盤官方 NAV。→ 第 t 列用**實際公布時點**對齊，fwd 從可交易日起算（沿用 t+1 開盤慣例）。
3. **樣本內過擬合（980T/adopted-44 老毛病）**：台股純股票型可交易 ETF N 太小，橫斷面統計力弱。→ 優先當**單一「擁擠指數」時序訊號**，不做橫斷面 long/short。
4. **regime 依賴**：edge 集中多頭末端，2022 空頭散戶已離場即失效。→ 跨 regime 分層驗證。
5. **折溢價機械性假訊號**：流動性差的小型 ETF 折溢價常態偏大（造市/套利不足非情緒）。→ 按規模/成交值過濾，只取大型 ETF。
6. **申贖與公司行動混淆**：除息、成分股調整、實物/現金申贖會造成 SHOUT 非情緒性跳動。→ 剔除公司行動日。
7. **高股息 ETF 結構性申購潮（2023–26）**：長期單邊申購是產品週期非可交易情緒。→ z-score 去趨勢窗口不可太短，否則把結構誤判為訊號。
8. **DSR 門檻**：OOS Sharpe 漂亮不算數，要過 Deflated-Sharpe 且 permutation vs 同曝險隨機。

---

## 7. Gap-fill：S2 折溢價（premium/discount）NAV 源窮盡調查（2026-07-30）

**任務**：補 S2 折溢價（市價 vs 官方每日淨值），測其超短 mean-revert 與「笨錢擁擠」反向燈。
**結果 verdict：`待補`（實跑 = 資料不可得的証偽）** — 折溢價需**歷史每日官方 NAV**當分母；經以下全面探測，**免費/已授權管道皆無台股 ETF 歷史 NAV**，故 S2 無法在本專案紀律下乾淨計算。此非「未做」，而是「已窮盡、確認無源」。

### 已探測來源（全部真呼叫、逐一記錄）

| 來源 | 探測內容 | 結果 |
|------|----------|------|
| **FinMind** | 全 dataset enum（實抓 103 個 dataset 名）grep `nav/net/asset/etf/fund/premium` | **無任何 NAV dataset**。僅有 `TaiwanStockActiveETFHolding/Info`（主動式 ETF 持股，非 NAV）。四個猜名 `TaiwanStockETFNetAssetValue / TaiwanETFNAV / TaiwanStockETFReport / ETFNetValue` 全回 HTTP 422 enum 拒絕。**確認無 NAV。** |
| **FinMind 指數** | `TaiwanStockTotalReturnIndex`（想用「台灣50報酬指數」建 0050 price/index 折溢價 proxy） | 僅含 `data_id=TAIEX`（大盤報酬指數 46583.96）。`TW50/0050/IX0001/…` 皆 0 列 → **無台灣50指數**，無法建市值型 ETF 的 NAV proxy。 |
| **TEJ（E-SHOP 斜槓）** | `EWPRCD`（0050 有日 K，含 `close_adj`）；`EWIPRCD`（指數表，實抓單日=**僅 16 檔**指數 `IR0001..IX0118`） | ETF 市價分子有，但 **16 檔指數無台灣50**（只有大盤/電子/金融等寬基）；TEJ 方案表（EWPRCD/EWIPRCD/EWIFINQ/EWSALE）**無 NAV 表**。→ 分母仍缺。 |
| **TWSE OpenAPI**（免 token） | swagger 全表 grep ETF/NAV/fund；實抓 `ETFReport/ETFRank` | 僅 `ETFRank`（ETF **交易戶數**排名，欄=NumberofTradingAccounts）+ `MI_QFIIS`（外資持股）。**無 NAV / 折溢價**，且 OpenAPI 一律**當日快照無歷史**。 |
| **TWSE rwd/pcversion**（`ETF/etfInout`） | 三個路徑 × 帶/不帶 date | 皆回 **HTML 頁面**（非 JSON data endpoint），無法程式化取歷史折溢價。 |
| **TPEX OpenAPI** | `tpex.org.tw/openapi/v1/` | 本環境 **SSL CERTIFICATE_VERIFY_FAILED**（Missing Subject Key Identifier），無法取。 |
| **本地 `stocks.db`** | `etf_holdings_meta.nav`（第三方估算淨值）× `stock_daily_bars`（ETF 市價） | `nav` 僅涵蓋**主動式 ETF**（00980A/00981A/…，各 ~22 列非空），**與本維度宇宙（0050/0056/00878）零交集**；市價僅 0050/0056（2025-05→，~14 月）。**價與淨值無重疊、且淨值非官方**→ 無法建面板。 |

### 為何不硬補（誠實取捨）

- **最乾淨的市值型 proxy（0050 price ÷ 台灣50報酬指數，detrend 去費用漂移＝折溢價）本可跑，但兩大 index 源（FinMind/TEJ）皆無台灣50指數**，改用 TAIEX 當分母則成分（全上市 vs 前50大）差異會被大/小型股離散度污染，非乾淨折溢價 → 呈報即失實，不做。
- **真正的「笨錢擁擠」froth 在高股息 ETF（00878/00929/00940 於 2023–24 溢價數 %）**，其追蹤的是**客製指數**、無公開歷史指數可對 → 即使有市值型 proxy 也打不到最有故事的標的。
- 剩餘唯一路徑＝**爬玩股網/MoneyDJ 折溢價頁**（HTML、逐檔逐日、易碎），配額/穩健度成本高，且母維度 S1+S3 已定調**非真 alpha**、S2 最有價值標的又拿不到 NAV → 邊際期望值低，判定不值得起爬蟲。

### 落層與 champion 搭配（維持事前設計，未變）

S2 事前設計＝**L1 觸發閘門**（Xu 2022：溢價日的申購 / 折價日的贖回才算數）+ 超短線 mean-revert 前兆；當 champion（`fut_foreign_oi_z60`）的**反向情緒 overlay**。**因 NAV 不可得，S2 未進任何 L0–L3 生產層，掛 `待補`**。若日後接得官方每日 NAV（TPEX ETF 專區修好 SSL、或投信官網申報頁），可直接套 `etf_flows_study.py` 既有 `eval_signal` 全套證偽管線（IS/OOS 70/30 + permutation + DSR + 去 champion 共線 + regime）跑 S2。

**S2 覆蓋誠實**：官方每日 NAV = 0 檔 × 0 日（不可得）；市價分子 = 局部可得（0050/0056 本地 ~14 月、TEJ EWPRCD 全檔）；proxy 指數分母 = 無台灣50。→ **S2 折溢價 real_result 無數字，verdict=待補。**

---

*非投資建議（Not investment advice）。本報告為量化研究流程之資料盤點與方法論設計，所有數字為研究性回測/自檢，非對任何 ETF 或標的之買賣建議。*
