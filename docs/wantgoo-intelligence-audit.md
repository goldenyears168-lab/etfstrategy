# 玩股網情報稽核 · 對照本專案缺口

> **產出**：2026-07-31 排程任務 · 多層 agent 調查（8 個 L1 agent × 逐頁抓取 + 本機實測驗證）
> **問題**：玩股網哪些頁面/數據/評論真的有價值，能天天搭配本專案資料庫使用？我們缺什麼？
> **方法**：逐頁 WebFetch + 反解前端 JS bundle 取出 API 端點 + **本機 Python 實測**（不是憑印象）
> **一句話結論**：玩股網**沒有任何 TAIFEX/TWSE 以外的原始資料**，但它有**三樣我們真的缺的東西**，
> 其中一樣（個股融資維持率）已用 20 檔股票證明我們現行的替代方案是錯的。

---

## 0. TL;DR — 只讀這段

| # | 發現 | 證據等級 | 動作 |
|---|---|---|---|
| **1** | **個股融資維持率**免費、全宇宙、490日可取。我們現行 `stock_maint_proxy.py` 相關 r=0.72、MAE **17.1pp**、<130%警戒**誤報 340 次 vs 命中 134 次** | ★本機實測 20 檔 × 7,875 日對 | **直接換掉 proxy** |
| **2** | **董監持股% + 400張大戶% + 散戶持股%**（週頻）免費可取 — 我們整個「持股存量結構」軸是空的 | ★實測 8069 有真值 | 建 weekly 面板（優先走 FinMind `TaiwanStockHoldingSharesPer`） |
| **3** | **每日 poll `/stock/all-monthly-revenue` 可自建營收公布日(annd)** — 零成本複製 TEJ 的 PIT 欄位，還順便得到公布進度 | ★實測 n=2628 單月快照 | 起 cron，append-only |
| **4** | **均線扣抵值**：台股特有、機械性可前視，四套框架（Weinstein/Minervini/VCP/RRG）全都沒有 | 概念確認 | 用現有日線即可算，零新資料 |
| **5** | 「小外資 = 外資 − 前五大特法」是我們已驗證因子的乾淨拆解 | JS 原始碼公式 | 用 TAIFEX 全史重算三腳 |
| **6** | **分點資料無回補價值** — 玩股網上限「近一年」，比我們現有 2024-07 更淺，且要 $2,388/年 | 官方頁面文字 | 放棄這條路 |
| **7** | 部落格 28 位作者中，**4 位每日必看**、**13 位明確有害**（付費牆+倖存者偏誤行銷） | 讀 15 篇全文 + 促銷 API | 白名單 poll，見 §4 |
| **8** | **我們自己的運營迴圈才是最大問題**：無隔日驗證、無績效追蹤、~2,100 行報告死碼、4 份主文件過期 | ★本機驗證 | 見 §5，優先於任何新資料源 |
| **9** | **主 ingest 管線沒有排程** → 融資券/借券/當沖/外資持股/技術指標 5 張表停在 **2026-07-07~09**（三週前），且宇宙只有 166~219 檔 | ★本機驗證 | 見 §3.9 · §5.4-P0 |
| **10** | **玩股速報底層 API 未被擋**，含**外資台指期淨OI**（我們唯一存活的因子）、扣ETF維持率、十大交易人、公股托盤 | ★本機實測 200 | §2.3 |
| **11** | **夜盤層可取**：台指期盤後 `WTXP&`、富台指 `STWN&`、摩台指 `S2TWZ1`、費半期貨盤後 —— 補「台美脫鉤」gate 缺的隔夜領先；**VIXTWN 每日可抓**（全史仍缺） | ★本機實測 | §2.4 |

---

## 1. 玩股網的本質（先講清楚，避免高估）

- **沒有獨家原始資料。** 全站欄位對接 TWSE / 櫃買 / TAIFEX / 集保盤後公開資料。
- **沒有回測功能。** `/screener` 原始碼中 `回測/績效/勝率/backtest` 出現 **0 次**。舊版績效頁 `ob.wantgoo.com/hottipperformanceanalysis.aspx` 已 404 下線。
- **價值在三處**：(a) 加值計算的**定義**（可自建）、(b) 少數**我們沒建的資料軸**、(c) 幾位作者提供的**制度機制敘事**（資料庫推不出來的因果）。

### 1.1 存取方式（實測）
沿用專案既有 `_WANTGOO_HDR` 配方即可：
`User-Agent` + `Accept: application/json` + `Referer`(同源頁) + `X-Requested-With: XMLHttpRequest` + `Sec-Fetch-Site/Mode/Dest`

**開放 vs 封鎖是系統性的**，不是速率限制：
- ✅ 開放：全市場橫斷面 `all-*` 系列、`historical-*` 系列（推測有 CDN 邊緣快取）
- ❌ 封鎖：即時類與 `-data` 尾綴類，Python/curl（含 HTTP/2、cookie warm、完整 Chrome header）一律 **400**，同一 URL 在真瀏覽器 200 → 推測 TLS/JA3 指紋層
- **但封鎖的那些全部是 TAIFEX/TWSE 公開資料的重述，沒有必要爬。**

---

## 2. 確認可用的端點清單（本機實測 200）

### 2.1 全市場橫斷面快照 — ⚠️ **只有最新一期，無歷史、無公布日**
| 端點 | n | 關鍵欄位 |
|---|---|---|
| `/stock/major-investors/all-large-shareholding-rates` | 4988 | 大戶持股級距% (2~15+) |
| `/stock/major-investors/all-small-shareholding-rates` | 4988 | 散戶持股級距% |
| `/stock/major-investors/all-main-power-concentrations` | 2502 | 主力集中度 1/3/5/10/20/60/120日 **+ previous（可算變化）** |
| `/stock/all-monthly-revenue` | 2628 | date(所屬月), MoM%, YoY%, 累計YoY% |
| `/stock/all-cumulative-revenue-trend-months` | 3034 | 營收**連續成長/衰退月數** |
| `/stock/all-top-4-eps` · `all-top-8-eps` | 8052 / 15985 | year, season, eps |
| `/stock/all-profit-margin` | 4027 | grossProfit, netOperatingProfit, nopat, revenue |
| `/stock/all-top-4-roe-roa` | 7833 | roe, roa |
| `/stock/all-{gpl,nopat,oil}-rate-new-high-low-seasons` | 3093 | 三率**創新高/低季數** |
| `/stock/all-company-profile-data` | 2717 | 股本、流通股數、成立日 → **可算流通市值、公司年齡** |
| `/stock/all-turnover-rates` · `all-latest-net-worth-per-share` | 2297 / 2675 | 週轉率、每股淨值 |
| `/stock/all-warrant-count-data` | 855 | 權證檔數（造市關注度代理） |
| `/stock/dividend-policy/all-ex-dividend-data` | **11661** | 除權息**全史** + shareholdersMeetingDate + paymentDate |
| `/stock/etf/all-constituent-data` | 83 | ETF→成分股（可算**被動買盤覆蓋度**） |
| `/investrue/all-alive` | 6208 | 全商品 universe + type + market + **industries** |
| `/investrue/all-quote-info` | 5807 | 全市場報價 |
| `/investrue/all-daily-candlestick-beta-value-indicator?tradeDate=<ms>` | 2696 | beta 1M/3M/6M/1Y |

### 2.2 時間序列（有歷史深度）
> **關鍵參數**：日期一律 **epoch milliseconds**（傳 `2026-07-31` 會 400 — 這是最容易卡住的地方）；
> `topDays`/`top` **上限 1250**（傳 1251 → 400）。

| 端點 | 深度（實測） | 欄位 |
|---|---|---|
| **`/stock/historical-bull-bear-arrangement-data?beforeTradeDate=<ms>&investrueIds=0000`** | **6796 列 · 2019-07-31→2026-07-28（全站最深，7年）** | date, indicator, value；indicator ∈ `{Long,Short}Term{Long,Short}ArrangementRate`（多空頭排列比例） |
| `/stock/historical-advance-decline-line-data?beforeTradeDate=<ms>&topDays=1250` | 1250日 · 2021-06-14→2026-07-28 | raiseCount, fallCount（騰落線原始家數） |
| `/investrue/0000/historical-daily-candlesticks?before=<ms>&top=1250` | 1250日 · 2021-06-07→ | OHLC + volume + millionAmount |
| **`/investrue/000-/historical-daily-candlesticks?before=<ms>&top=1250`** | 1250日 · 2021-06-07→ | **大盤扣除台積電**合成指數（TWSE/FinMind 都沒有）⚠️ 演算法未公布 |
| `/investrue/wtx&/historical-daily-candlesticks?before=<ms>&top=490` | 490根 | 台指期日K |
| `/stock/major-investors/all-net-buy-rank-data?tradeDate=<ms>` | 645檔/單日 | 主力 netBuySell1Day / 15Day |
| `/index/all-industries-dividendyield-per-pbr` | 64類股 | ⚠️ **見 §3.8 資料陷阱** |
| **`/stock/{id}/margin-trading/historical-lending-balance`** | **490日** | 融資餘額 + **`marginRatio` ← 個股維持率** |
| `/stock/-ETFA/margin-trading/historical-lending-balance` | 490日 | 扣除ETF大盤維持率（**專案已在用**） |
| `/stock/{id}/margin-trading/historical-borrowing-balance` | 490日 | 借券餘額 |
| `/stock/{id}/margin-trading/historical-foreign-short-lending` | 490日 | 外資借券賣出 |

### ★2.3 玩股速報底層 API — 未被擋，且正好是我們在用的欄位
`/newsletter/daily/historical-*?tradeDate=<ms>`，Referer 用 `/newsletter/daily/fast`。**全部實測 200**：

| 端點 | n | 實測值 |
|---|---|---|
| `historical-foreign-futures-oi-data` | 5日 | `openInterestLotBalance` = **−81,017 口** ← **這正是我們研究中唯一存活的因子（外資台指期 positioning）的乾淨 JSON 源** |
| `historical-margin-trading-exclude-etf-data` | **20日** | 扣除ETF `lendingBalance` + `borrowingBalance`（比現用的 `-ETFA` 端點多給融券） |
| `historical-futures-large-traders-data` | 21日 | `top10Balance` 十大交易人淨部位 + close |
| `historical-public-bank-buy-sell-data` | 7日 | `netAmount` **八大行庫（公股托盤）買賣超** |
| `historical-option-put-call-ratio-data` | 2 | `volumeRatio` / `openInterestRatio` |
| `historical-daily-candlesticks?investrueId=0000` | — | 加權日K（**僅 0000 白名單**） |

（`historical-retail-indicator-data` 實測 400，參數未解出。）

### ★2.4 全市場快照 + 夜盤（補 VIXTWN 與台美脫鉤缺口）
| 端點 | 實測 |
|---|---|
| **`/investrue/historical-all-quote-info?tradeDate=<ms>`** | **4,575 檔**單日 OHLCV，**內含 `VIXTWN`**（實測 open 42.33 / high 46.96 / low 40.48 / close 44.31）。回溯窗約 10 個交易日 |
| `/investrue/all-quote-info` | 5,807 檔即時，**含台指期盤後**：`WTXP&` 41859 · `WMTP&` · `WTMP&` · **`WSXP&` 費半期貨盤後** · `WCDFP&` 台積電期貨盤後（皆台北 05:00 快照） |
| `/global/all-quote-info` | 675 檔，**`STWN&` 富台指 3574** 與 **`S2TWZ1` 摩台指 1897.7** 皆 `isLongRealTime=true`（SGX T+1 夜盤完整入帳）；`M1ES&`/`M1NQ&` E-mini；共 31 檔夜盤標的 |

> **VIXTWN**：我們現有只有 62 天（2026-05-04 起）。全史 wantgoo 也補不了（per-symbol K線端點被擋，
> TAIFEX 日線全史是付費商品 NT$3,000/半年），**但每日 poll 這支就能讓缺口不再擴大**，10 個交易日容錯窗夠寬。
> **台美脫鉤 gate**：現在能在同一時點同時拿到「美股怎麼走」（`M1ES&`/`M1NQ&`/`SOX`）與
> 「台股自己怎麼定價」（`WTXP&`/`STWN&`/`S2TWZ1`/ADR），這是這個 gate 過去缺的**隔夜領先層**。

### 2.5 只有 HTML（WebFetch 可讀，無 JSON）
- `/stock/{id}/major-investors/concentration` — **週頻**：400張大戶% / 外資% / 投信% / 自營% / **董監持股%**，約1年
- `/stock/calendar/investors-conference` — 法說會，前瞻約1個月，附簡報檔連結
- `/stock/calendar/dividend-right` — 除權息預告，前瞻約2個月

---

## 3. 對照我們的缺口

### ★3.1 個股融資維持率 — 唯一「已證明我們現在做錯」的項目

記憶中「個股/整戶維持率」是 FinMind 與 TEJ **雙雙缺漏**的殘餘缺口。實際情況：
- FinMind 只有市場級 `TaiwanTotalExchangeMarginMaintenance`
- 我們因此寫了 `scripts/research/chip_macro/stock_maint_proxy.py`，用
  `維持率 ≈ 166.7% × 現價 / 融資流量加權成本` 近似，且**只涵蓋 20 檔硬編碼股票**
- 腳本自己註明：「方向性排名參考，非精確維持率」

**玩股網免費提供真值**：`/stock/{id}/margin-trading/historical-lending-balance` 的 `marginRatio`，
**490 交易日回溯到 2024-07，實測 11/12 檔有資料**。

**我用 proxy 那 20 檔做了對帳（7,875 個股票-日配對，2024-07-09→2026-07-29）：**

| 指標 | 結果 |
|---|---|
| 相關係數 r | **0.720** |
| 平均誤差（proxy − real） | **+8.1 pp**（系統性高估＝低估風險） |
| 平均絕對誤差 MAE | **17.1 pp** |
| 誤差 p10 / p90 | −15.9 / **+42.2 pp** |
| **<130% 警戒** | 真實 163 日次 · proxy 觸發 474 日次 → **命中 134 · 誤報 340 · 漏報 29（漏報率 18%）** |
| **<150% 警戒** | 真實 1051 · proxy 1635 → 命中 839 · 誤報 796 · **漏報 212（20%）** |

**白話**：proxy 每發 3 次 <130% 警報，有 2 次是假的；同時還漏掉 18% 的真事件。
最新一日實例 — proxy 說旺宏 130%（剛好卡在門檻），真值 **106.8%**（早已過追繳線）；
國巨 proxy 119% vs 真值 **102.4%**。

個股層級偏誤最大者：華邦電 +22.2pp、南亞科 +21.9pp、景碩 +19.3pp、旺宏 +17.6pp。

> **順帶佐證我們自己的發現**：`00631L` 的 marginRatio 最低到 **8.1%**，
> 直接印證「含ETF口徑被槓桿ETF灌爆」這個 C3 研究的核心論點。

**動作**：
1. 用真值取代 proxy；`stock_maint_proxy.py` 降級為離線 fallback 或直接退役
2. 新指標（我們完全沒有的）：**橫斷面斷頭廣度** = 全市場 marginRatio <130% 的檔數/比例。
   目前的 C3 只有市場級單一數字；橫斷面版本能分辨「整體溫和但特定族群斷頭」
3. ⚠️ 採用前先確認玩股網 marginRatio 的口徑（是否扣除ETF、上櫃5成 vs 上市6成）——
   對 20 檔的偏誤方向不一致（台塑化/國巨為負），暗示口徑可能隨標的而異

### 3.2 持股存量結構 — 整個軸是空的
我們的籌碼研究幾乎全是**流量**（分點、法人日買賣超）。以下是**存量**，完全不同的資訊軸：
- 400張以上大戶持股% / 20張以下散戶持股% / 董監持股%（**週頻，集保**）
- 經典假說：大戶比↑ × 散戶比↓ = 籌碼沉澱

**但別從玩股網抓** — 原始出處是集保 TDCC，且 **FinMind `TaiwanStockHoldingSharesPer`（15級距週資料）我們的 token 已涵蓋**，
PIT 乾淨（集保公布日明確）、歷史更長。玩股網只用來對帳。

⚠️ **陷阱**：玩股網「大戶」門檻隨股價浮動 —— `股價>50元 用400張、否則用1000張`（JS 原始碼 `window.deal > 50 ? moreThan400 : moreThan1000`），
未在頁面揭露。任何股票跨越 50 元都會造成序列不連續。**不要直接用他們的大戶比做時序研究。**

### 3.3 董監質押 — 玩股網完全沒有
確認：有董監**持股**，無**質押**。這條缺口要另尋（公開資訊觀測站月報）。

### 3.4 全宇宙 rev_yoy 的 PIT — 有一條零成本的路
**玩股網全站沒有任何「公布日/公告日」欄位**（2330 月營收頁 `公布` 出現 0 次）。直接用會引入前視偏誤。

**但 `/stock/all-monthly-revenue` 可以拿來自建 annd**：
該端點是單月快照（實測 2628 檔**全部** `date=2026-06-01`）。每月 1–10 日公布窗口內，
**已公布的公司 `date` 會翻月、未公布的停在舊月** → **每日 poll，「首次翻月」那天就是實際公布日**。

這等於零成本複製 TEJ 的 `annd`，而且**可以拿來獨立驗證 TEJ annd 的正確性**
—— 這是對 `rev_yoy_3m` 最有力的對抗式檢查。缺點：只能從今天起累積，補不回歷史。

### 3.5 分點 — 確認死路
玩股網 `/event/advanced` 明文：「券商全分點資料，統計區間延伸至**近一年**」，進階會員 $2,388/年。
以 2026-07-31 計只回溯到 ~2025-08，**比我們現有的 2024-07 更淺**。零回補價值。
有價值的只有**公式**（可在我們自己的 tape 上重算回 2024-07）：
- 主力 = 前15大券商買賣超
- **家數差 = 買超券商家數 − 賣超券商家數**
- **籌碼集中度 5日 = (買方前15名5日總買超 − 賣方前15名5日總賣超) / 5日總成交量**

### 3.6 台指期籌碼 — 「小外資」拆解
JS 原始碼公式：`小外資 = 外資淨未平倉 − 前五大特定法人淨未平倉`。
這把我們**已驗證的**「外資台指期淨OI」切成兩腿（前五大特法多為投行避險/套利盤 vs 殘差外資）。
若 alpha 集中在殘差腿，等於**不新增資料源就提升既有因子訊噪比**。用 TAIFEX 全史即可重算。

> ⚠️ **「散戶多空比」不是新資訊。** JS 原始碼顯示
> `(散戶多單−散戶空單)/總OI ≡ −(三大法人淨多倉)/總OI`。
> 它代數上就是法人 OI 的負值。真正有價值的只有 **/總OI 這個標準化形式**
> —— 值得回頭套用到既有大台外資 OI 因子（raw 口數 vs OI-normalized，測哪個 OOS 較穩），成本近乎為零。

### 3.7 值得偷師、我們沒有的因子（依價值排序）
1. **均線扣抵值（月線/季線扣抵）** — 台股特有。今天就能算出未來 N 天會滾出均線窗口的價格，
   因此能**確定性預告均線斜率轉折**（是算術不是預測）。Weinstein/Minervini/VCP/RRG 四套框架全都沒有。
   **零新資料需求**，對 Stage 1→2 轉折擇時可能直接有用。**我認為這是最被低估的一條。**
2. **ETF 成分股反查** — 「這檔被幾檔 ETF 持有」= 被動資金需求代理。我們完全沒有被動流量這一軸。
3. **基本面「創新高」家族**（營收/EPS/三率創新高次數）— 我們的 `rev_yoy_3m` 是**成長率**形態；
   「創新高」是**水準**形態，數學上不等價，且不受基期效應污染。用現有 TEJ 資料即可算。
4. **營收連續成長月數（streak）** — `rev_yoy_3m` 的持續性形態，天然過濾單月暴衝雜訊。
   Phase-5 結論是 rev_yoy 全宇宙弱化，streak 版可能是讓它在全宇宙存活的變形。
5. **Beta 分桶** — 把我們最強的發現（regime-conditioning）從時序搬到橫斷面。
6. **分母正規化**：投本比（投信買超/股本）、融資使用率（餘額/限額）、券資比。
   已知 `rev_yoy_3m` 的 alpha 集中在中小型股 → 正規化對中小型股影響最大，這是**對既有訊號的免費改良**。
7. **多空頭排列比例**（`historical-bull-bear-arrangement-data`）— **2019-07 起 6,796 列，全站最深**。
   現成的市場廣度 regime 變數，比自己重算省事（但均線參數未公布，要複製得先猜）。
8. **52 週新高−新低家數差**（`/stock/new-highs-vs-new-lows-index`）— 定義透明（頁面明寫），經典廣度指標，自算即可。

### ★3.8 三個「不可信 / 會害你判斷錯」的地方（按嚴重度）

1. **類股與大盤的 PE / PB / 殖利率是月頻值，卻放在日報價表且不標 as-of。**
   實測 `/index/all-industries-dividendyield-per-pbr` 回傳 `date = 2026-05-31`，
   但頁面在 07-31 仍照常顯示這組數字 → **直接日頻使用會吃到 2 個月陳舊資料**。
   這是會實際造成錯誤判斷的資料陷阱，不是命名問題。
2. **市場寬度的 85% / 15% 頭底門檻沒有任何統計依據或樣本數揭露**（拍腦袋整數）。
   指標本身（% above MA）是國際通用的，可用；**門檻必須自行重新校準**。
   「散戶多空比」同類：無公式、無「散戶」定義、無統計佐證、歷史僅 1 年。
3. **規則不公布的黑箱**：「飆股排行」「飆股檢測」（純行銷命名）、「小外資」（定義未公布，
   但 JS 原始碼洩漏 = 外資 − 前五大特法）、**「大盤扣除台積電」**（有實用價值但演算法不透明、
   站方隨時可改而不通知 → **可當監控燈看「權值股綁架 vs 真實廣度」，但不該當回測輸入**）。

### 3.9 我們自己的宇宙比想像中窄（本次盤點的意外發現）
只有日線（2,504 檔）與分點（2,355 檔）是全宇宙。其餘個股籌碼**極窄**：

| 表 | 宇宙 | 最後更新 |
|---|---|---|
| `stock_margin_daily`（融資券） | **僅 219 檔** | 2026-07-29 |
| `stock_lending_daily`（借券） | 170 檔 | **2026-07-08** |
| `stock_daytrade_daily`（當沖） | 171 檔 | **2026-07-08** |
| `stock_shareholding_daily`（外資持股） | 200 檔 | **2026-07-07** |
| `stock_technical_daily` | 166 檔 | **2026-07-09** |
| `stock_fundamental`（月營收） | 170 檔 | 2026-07-03 |

**原因已查明**：主 ingest 管線 `scripts/daily_sync.sh` / `1630收盤雷達.command`
**不在任何 enabled launchd job 裡**，7 張核心表全靠人工執行 → 停在三週前。
而有 launchd 的分點與 1 分 K 就更新到 07-27。
**這比「缺哪個資料源」嚴重得多** —— 玩股網式的「全市場任一檔都能查」我們現在做不到。
另注意 `stock_branch_daily`、`stock_block_trade` **兩張表都是 0 列**（有 sync code，從沒跑成功）。

### 3.10 明確不值得的
KD/RSI/MACD 金叉死叉、K棒型態、價量四象限（過度擁擠，我們的 VCP/Minervini 已有更嚴謹版本）；
「股價蛻變基準線」「動態平衡讀數」（公式不公開、黑箱，不可納入 —— 但前者內部變數名 `MainForceStockCost`
洩漏了它就是**主力成本線**，概念可自建）。

---

## 4. 部落格 / 社群：誰有用、誰沒用（具名）

調查 28 位作者、讀 15 篇全文。**結構性發現**：`/blog/{id}/promotion` 乾淨地把作者分成兩群 ——
10 位帶**社團銷售橫幅**，玩股-前綴的編輯帳號**完全沒有**。這個分界幾乎完美預測內容品質。

### 每日必看（4 位）
| 作者 | ID | 為什麼有用 |
|---|---|---|
| **玩股華安** | 407988 | 無社團、無付費文。7/29 直接處理**扣除ETF大盤維持率 133.12%**，並拆解制度節奏（跌破130%發追繳 → 2營業日補繳 → T+3開盤市價強制賣出）。**這正是我們資料庫算不出來的機制敘事**，且與 C3 研究直接對得上 |
| **玩股摸金** | 203663 | 盤後 recap 給**可自行複製的期貨斷頭算式**：`(636000−488000)/200 = 740點` 觸發追繳、`636000×0.75/200 = 2385點` 代為砍倉 |
| **玩股講客人** | 343059 | 全站方法論最好。7/28 鴻海文**回頭對帳自己去年的預測**、主動拆自己的地雷、給明確翻轉條件。⚠️ 但對批評者語氣具攻擊性，且有**選擇性回顧偏誤**（只挑對的講） |
| **玩股小博士** | 98845 | 用權重反推自洽（台積電每漲1元=大盤7.95點）、**主動承認前次算錯**、結論反共識且可驗證 |

備選第5位：**玩股特派員** (144623)，產業基本面懷疑論寫得紮實。

⚠️ **身分揭露**：玩股講客人與玩股小博士的文末嵌了執行長楚狂人的 FB 連結，玩股華安/摸金/特派員沒有
→ 前兩者掛在執行長行銷管線下，內容品質仍高但**別當獨立第三方意見**。

### 明確不必看（具名，共 13 位）
| 作者 | 紅旗 |
|---|---|
| **買飆股就是人生最大財** (277398) | 50篇中48篇付費；標題「必賺投資組合」「100%必噴股票」；99訂閱卻 2,940 筆付費銷售 |
| **◆Sage Mao** (75003) | 10,825篇、2.07篇/日；內文一行+一張圖，同檔連貼三週；**把大樂透開獎預測當付費文賣** |
| **短線飆派軍團長** (23795) | 免費文全是「抓到+46%飆股」的**事後回填廣告** → 結構性倖存者偏誤 |
| **玩股神探** (67867) | **偽裝成教育的廣告**，連續三篇全導向 `/club/50` |
| **OP MAN** (359756) | NT$37,800/360天，宣稱「1362%績效」「勝率>90%」 |
| **軌道鞅** (73359) | 公開文全是格言，零可驗證內容，社團宣稱「勝率超過九成」 |
| **霍立** (161002) | **最後一篇 2024-10-03**，仍掛在推薦榜上 |
| **謝球爸** (69233) | 連續50篇標題一字不差（機器人日誌） |
| **enjoyhsu** (235973) | 「大數據金融占星」（土星逆行、金火相刑） |
| 老漁夫 / 期俠 / Penny / 理財周刊 / jackie chen | 停更、標題黨、或格言+導流 |

### 正確用法
1. **事件標註（最高價值，馬上可做）** — 資料庫看得到 7/28–29 大跌 3,594 點，但算不出「為什麼是早盤」。
   摸金給的答案可複製：台指期 7/28 已逼近 2,385 點砍倉門檻（差 91 點），所以 7/29 只要再跌 91 點就觸發開盤市價砍倉。
   **這種制度性機制純數據推不出來。**
   做法：每日 `GET /blog/daily-featured-data?page=1`（免費、免登入、回傳 JSON 含 memberId/title/summary/publishTime），
   白名單過濾 `memberId ∈ {407988, 203663, 343059, 98845, 144623}`，
   再用 `/blog/{id}/post/{postId}/detail` 取全文，依 publishTime 對齊日線異常日，存成 event 標註表。
2. **假說記分板** — 這 4 位常寫下明確翻轉條件。建 `hypothesis(標的, 方向, 條件, 檢驗日)` 表，
   到期自動打分。跑一年就知道誰真有 edge —— 比看訂閱數可靠得多。
3. **不要做的**：`/social-wall` 不是社群牆，JS 硬編 `memberId=119` = **執行長一個人的廣播牆**，n=1，
   反應數衡量的是作者行為不是市場情緒。要情緒指標得去 PTT 股板。

### 4.1 新聞：可掛個股，且有 12 年語料（我們目前新聞資料 = 0）
盤點確認我們**本地無任何新聞資料表或快取** —— 唯一新聞路徑是 cloud routine 21:30 即時 web search，
**不落庫、不可回測**。玩股網的新聞層可以填這個洞：

| 用途 | 端點 | 說明 |
|---|---|---|
| 每日增量 | `/news/category/{頭條\|台股\|國際}/list?page=N` | 撈 id |
| 全文 | **`/news/{id}/detail`**（未被擋） | `id, time(epoch ms), category, headline, summary, author, story(HTML全文), tags[]` |
| 歷史回填 | sequential id 掃描 | 實測 id=100000→2014-06-09、400000→2016-05-05、800000→2018-06-04 皆 200 → **約 12 年語料** |
| 掛個股 | `tags[]` 是**中文名**不是代號 | 用 `/investrue/all-alive`（6,208 檔 id↔name，未被擋）做名稱→代號映射；或解析 `/stock/{code}/news?page=N` HTML（JSON 版被擋） |

存量：頭條 25,955 篇 · 台股 14,666 · 國際 8,758。
⚠️ **來源是鉅亨／聯合／中時轉載**（`author` 欄實測），非自製；同一事件多篇重複，需去重。

### 老鳥說 / 玩股速報
- **老鳥說 `/laojiao`：不值得。** 不是電子報，是課程商城。12 個專案中 5 個是執行長本人的，佔總訂閱 **92%**（6,033/6,535）。
  配套 9 個社團的宣稱語全部踩紅旗（「勝率超過90%」「日均穩吃100點」「累積獲利勇破10億」），未見風險揭露。
- **玩股速報 `/newsletter/daily/fast`：不必訂，但抓其中一欄。** 法人買賣超、期貨OI、P/C比我們 FinMind+TWSE 全都有且能自己回測。
  唯一例外是**「大盤扣除ETF資券進出」** —— 與其訂電子報，不如直接排程抓 `/stock/margin-trading/market-price/taiex`
  （比我們現用的 `-ETFA/historical-lending-balance` 更即時）。

---

## 5. ⚠️ 最重要的一節：問題不在情報，在我們的迴圈

你說「專案與排程還是讓人不太放心，無法真的掌握情報」。稽核結果證實這個直覺是對的，
**但原因不是缺資料源，是運營迴圈沒閉合**。以下每一條都經本機驗證：

### 5.1 文件描述的系統 ≠ 實際在跑的系統
`docs/PRD.md` / `architecture.md` / `daily-operations.md` / `reports/README.md` 描述的
16:30 收盤主線（Facts/Regime/RRG/VCP/Copytrade）**完全沒有安裝**。
- `reports/daily/etf-daily/daily_brief.md` mtime = **6/22**
- `reports/daily/regime/daily_brief.md` mtime = **7/10**
- PRD §13 成功標準第 1 條「每交易日 16:30 後可讀 Facts 與 Regime daily brief」→ **不成立**

### 5.2 對照你的四個要求
| 要求 | 現況 | 證據 |
|---|---|---|
| 每天驗證前一天 | **0** | `run_signal_radar_replay.py` / `run_launchd_replay.py` 是**開發除錯工具**（docstring 自述 "writes sample launchd-style logs"），不做「昨天說X今天結果Y」比對。跨軌 ex-post 審計曾存在但**被主動刪除**（`evaluation-contract.md:11`） |
| 每天規劃隔天策略 | **0** | 唯一叫「隔日開盤風控 checklist」的 `src/operational_brief.py`（260行，有完整 `build_morning_checklist_items()`）**沒在跑** |
| 每天找出無法解釋的地方 | **~10%** | 只有雲端 LLM commentary（無驗證、只涵蓋外資輪動）。規則式解釋器 `chip_narrative.py` / `market_analytics.py` / `comment_engine.py` **全是死碼** |
| 每天學習 | **~50%** | 專題級累積很強（research.yaml 的 G1–G6 gate、主動推翻自己結論），但是**衝刺式**寫入不是每日增量；**70 個 status:active topic** 中多數已收斂，無 `updated` 欄位 |

### 5.3 已驗證的具體問題
- `reports/research/chip-macro/signal_history.csv` **只有 1 筆資料**（2026-07-29），且**沒有任何前瞻結果欄位** → 命中率算不出來
- `daily_tracker.py`（最有價值的 6 盞燈風控燈號）**沒有任何 launchd 入口** —— grep `launchd/`、`scripts/launchd/` 皆無
- `data/research/chip_macro/panel.parquet` 最後日期 **2026-07-29**，但 dashboard 於 07-31 產出 → **靜默落後 2 天，無警示**
- `scripts/sync_strategy_performance.py` 三重失效（目標 schema 已退役、年份寫死到 2026-06-18、追蹤的 4 條策略都不跑了）→ **目前沒有任何活的策略績效追蹤**
- ~2,100 行報告產生器是死碼，且測試還在跑死碼，給人「這東西活著」的假象

### 5.4 建議順序（**這些優先於任何新資料源**）
**P0 — 讓已有的東西每天跑**
0. **先修 ingest**：把 `scripts/daily_sync.sh`（`RUN_STOCK_MARKET_SYNC` / `RUN_CHIP_SYNC` /
   `RUN_SCREENER_DATA_SYNC` 三個開關）掛上 launchd。**這是所有事情的前提** ——
   目前 5 張核心籌碼表停在三週前，任何新資料源接進來都是蓋在沙上（§3.9）
1. 給 `daily_tracker.py` 建 launchd（比照 `crash-thermometer-daily.plist.template`），掛 18:00
2. 加資料新鮮度 gate：panel 最後日 vs 最近交易日差 >1 → 報告頂端紅字 + history 標 `stale=True`
   （`holdings_pulse_20260730.md:52` 印著「RRG 收盤：2026-07-14」，16 天前資料無警示，同樣問題）

**P0 — 補隔日驗證（最大缺口）**
3. `signal_history.csv` 加 outcome 欄位：在 `daily_tracker.py` 的 `append_history()` 前加
   `backfill_outcomes()`，對每列 date join T+1/T+5 IX 報酬，寫 `fwd_1d_pct` / `fwd_5d_pct` / `call_correct`。
   append-only + 事後 join = 無前視。命中率立刻可算
4. 新增 `scripts/run_daily_signal_review.py`：讀昨天的 evening digest
   （`evening_watch/digest_*.md` 已有結構化「★觸發 / ·無訊號」格式，可直接 parse），
   join 今天日線 → 「昨天 N 檔觸發、今日表現、滾動 20 日命中率」。掛 09:30 或併進 20:00 digest 頂部

**P1 — 清理誤導**
5. `src/operational_brief.py` 要嘛復活要嘛刪掉 —— 不能繼續讓名字完全符合需求的模組躺著沒人跑
6. 重寫 `reports/README.md` 與 `docs/daily-operations.md`，改成指向 `config/job_registry.yaml`（唯一經實機驗證的 SSOT）
7. `config/research.yaml` 加 `updated:` 欄位，把已收斂的 topic 批次改 `status: concluded`

---

## 6. 每日情報作業建議（把玩股網接進來的正確方式）

| 時間 | 動作 | 來源 | 用途 |
|---|---|---|---|
| **每日 07:00** | `/investrue/all-quote-info` 取 `WTXP&` `WSXP&` `WCDFP&`；`/global/all-quote-info` 取 `STWN&` `S2TWZ1` `M1ES&` `M1NQ&` + ADR | 玩股網 | **隔夜領先層 → 台美脫鉤 gate**（§2.4）。台北 05:00 已定價，早於任何現貨資訊 |
| 每日 08:00 | poll `/stock/all-monthly-revenue`，append-only 快照 | 玩股網 | **自建 annd**（§3.4）+ 營收公布進度 |
| 每日 18:00 | `/investrue/historical-all-quote-info?tradeDate=` 取 **VIXTWN** | 玩股網 | 讓 VIXTWN 缺口**不再擴大**（§2.4） |
| 每日 18:00 | `/newsletter/daily/historical-{foreign-futures-oi,margin-trading-exclude-etf,futures-large-traders,public-bank-buy-sell}-data` | 玩股網 | 外資期貨OI（唯一存活因子）、扣ETF維持率、十大交易人、公股托盤（§2.3） |
| 每日 18:00 | 抓全宇宙 `marginRatio` | 玩股網 | **橫斷面斷頭廣度**（§3.1）→ 進 daily_tracker 第 7 盞燈 |
| 每日 18:00 | `daily_tracker.py` + 新鮮度 gate + outcome backfill | 本地 | **隔日驗證**（§5.4） |
| 每日 20:00 | `daily-featured-data` 白名單 5 位作者 | 玩股網 | **事件標註**：解釋今天資料庫算不出來的異常 |
| 每日 20:30 | `run_daily_signal_review.py` | 本地 | 昨日訊號對帳 + 滾動命中率 |
| 每週六 | FinMind `TaiwanStockHoldingSharesPer` | FinMind | **持股存量結構**面板（§3.2） |

⚠️ `/investrue/all-quote-info` 與 `/global/all-quote-info` 目前無鑑權也無公開條款，
**站方隨時可能把它們關進 fingerprint gate**（per-symbol 路徑已經是）。
落地時務必保留 fallback：VIXTWN → TAIFEX 付費／MacroMicro；夜盤 → TAIFEX 盤後行情；外資OI／維持率 → FinMind。

**驗收標準**：三個月後 `signal_history.csv` 應該能回答
「chip-macro 六盞燈各自的 20 日滾動命中率是多少」「哪一盞在什麼 regime 下失效」。
現在這個問題無法回答 —— 這才是「無法真的掌握情報」的根因。

---

## 7. 誠實的限制
- 玩股網 ToS 未逐條審查；本文所有抓取為研究用途的低頻請求。進 production 前應確認條款
- `marginRatio` 的精確口徑（是否扣除ETF、上市6成 vs 上櫃5成）**未經官方文件確認**，偏誤方向在 20 檔中不一致 → 採用前需再驗
- 付費牆後的內容（王牌捕手、mike5566 等）無法評估，本文對其判斷僅基於標題與銷售中繼資料
- 全市場橫斷面端點**無歷史**，任何基於它們的研究只能從今天起累積，補不回過去
- 本次未涵蓋：`/stock/comparison` 完整欄位（需登入）、股東會行事曆前瞻天數、部分總經子頁

---

# 延伸調查（2026-07-31 第二輪 · 7-agent workflow）

> **補洞範圍**：第一輪 §7 自列「未涵蓋」的頁面家族（總經/macro、screener+comparison、選擇權/期貨深層、法說會/除權息行事曆、產業類股），
> 加上兩位第一輪未收錄的作者，以及**用今天的真資料實跑一次每日迴圈**當作「正確用法」的活範例。
> 方法同前：WebFetch 逐頁 + 結構化評分。**一句話**：頁面家族又證偽一批，只多撈到 **1 個真可用的前瞻事件源（法說會行事曆）**
> 與 **1 個真正正交的待驗因子（選擇權外資 net-delta）**；最大收穫是**活範例**證明「新聞＋2 位機制型作者」能把 DB 算不出的「為什麼早盤瀑布」講清楚，並產出 4 條隔日可證偽假說。

## 8. 未涵蓋頁面家族 — 逐一裁決（延伸 §3）

| 頁面家族 | 軸 | 我們有嗎 | 裁決 | 關鍵理由（一句） |
|---|---|---|---|---|
| **總經/macro** | 台灣 M1B−M2 貨幣供給差 | ✗ | **adopt（但不從玩股網）** | 台股領先 3–6 個月的經典指標、我們**最大真macro缺口**；玩股網**無 API**（總經頁全 404，只有部落格文），走 FinMind/央行，**月頻**進 panel 當 regime |
| 總經/macro | 美債 US10Y/US2Y(→2s10s) + 美元指數 DXY | ✗ | **adopt** | FinMind 不含美債/DXY；`/global/all-quote-info` **即時可取**（實測 US10-YR 4.647、DXY 100.20），開盤前定價 → 補 §2.4 台美脫鉤 gate 的**利率/美元腿**；歷史走 FRED |
| 總經/macro | USD/TWD、三大法人現貨買賣超歷史 | 部分/✓ | **skip** | FinMind/TWSE 全有且 PIT 更乾淨；玩股網 per-symbol 歷史全 **400 封鎖** |
| 總經/macro | 商品（銅/油/金） | ✗ | watch-only | `/global` 有即時，但對台股擇時邊際；波羅的海乾散貨 JSON 內**根本沒有**（只在 HTML） |
| **screener/選股** | ETF→成分股反查 | ✗ | **watch-only** | `all-constituent-data` 全宇宙可取（0050:51 檔…）但**無權重、無歷史、無公布日** → 只能當**靜態 tag**（`n_ETFs_holding`/指數權值旗標），**不是被動流量** |
| screener/選股 | ETF 規模/受益單位/NAV/折溢價（真被動流量原料） | ✗ | watch-only | `all-constituent-weight-data` 只回 SPA 殼（欄名洩漏但 JSON 未解出）；**受益單位日變動×價=申贖流量**才是真流量，需先確認端點才能建 |
| screener/選股 | 權證檔數 | ✗ | **watch-only（值得起 log）** | `all-warrant-count-data`（2330:1123）= 散戶關注度代理，我們沒有；但**無歷史**，只能從今天起累積 |
| screener/選股 | 週轉率/每股淨值/公司基本資料/screener 欄位 | ✓/部分 | **skip** | 全是我們 OHLCV/TEJ 已有或 TWSE 重述；`net-worth-per-share` 還有垃圾離群值(−134292)且無公布日=非 PIT |
| **選擇權/期貨** | **散戶多空比** | ✓ | **skip（冗餘已證實）** | 代數上 =−(法人/大額交易人期貨淨OI)/總OI，**同商品、換符號** → 加了就重複計算我們唯一存活因子。深度資料只值得拿來 A/B 測 raw口數 vs OI-normalized |
| 選擇權/期貨 | **選擇權外資 net-delta（call_net−put_net）+ 選擇權 OI P/C 比** | 部分 | **watch-and-test（唯一真正交候選）** | **不同商品**、無法由期貨OI導出，是本輪唯一正交新軸；走 FinMind `TaiwanOptionInstitutionalInvestors` 取深歷史，先算與OI因子相關性，再當第二腿丟進 regime+DSR，**過檢才收**（預期弱、有到期季節性） |
| 選擇權/期貨 | 三大法人期貨未平倉 decomposition（小外資/投信/自營/前五特法） | 部分 | watch-only | 同商品更細切，非新軸；只做第一輪 §3.6 提的「外資 vs 小外資(殘差)」拆腿測試 |
| 選擇權/期貨 | 外資期貨持倉成本/未實現損益 | ✗ | watch-only | 行為上新穎（(現價−成本)/ATR 可當投降 gate）但玩股網是**黑箱重建**；若做要**自算**（FinMind OI流量+價 VWAP） |
| 選擇權/期貨 | Max-pain/支撐壓力 T-table/倒莊監控 | ✗ | **skip** | 黑箱、無方法論、無乾淨時序；max-pain 預測證據薄弱且可自算 |
| **行事曆/產業** | **法說會前瞻行事曆** | ✗ | **adopt（本輪最佳新增）** | `calendar/investors-conference` 未來 5 週事件，**天生 PIT 乾淨**（排定日期事前已知，snapshot_date 即 PIT 戳）；我們事件語料=0。**HTML 無歷史 → 需每日 snapshot append，從今天起累積** |
| 行事曆/產業 | 除權息前瞻行事曆 | 部分 | watch-only | 機械性、TEJ 已涵蓋；只用來**避免把除息跳空誤判成真實報酬** |
| 行事曆/產業 | 除權息全史 `all-ex-dividend-data` | 部分 | **skip + ⚠️前視陷阱** | 其 `date`=**除息日不是公告日**、`shareholdersMeetingDate` ~90% null、**無公告日欄** → 任何「股利意外」研究用它=前視污染；只可拿股利數字對帳 TEJ |
| 行事曆/產業 | 類股 PE/PB/殖利率 | ✗ | **skip（陷阱已再確認）** | 全 70 列共用**單一時間戳 2026-06-01**（07-31 已陳舊 2 個月）、date 參數傳了回 **400**、無時序 → 不可做輪動擇時；要就走 TEJ/FinMind |

**§8 淨結論**：整個延伸頁面盤點只多出 **2 個真值得動的東西** ——
(1) **法說會前瞻行事曆**（adopt，需每日 scraper，補我們 0 事件語料）；
(2) **選擇權外資 net-delta**（watch-and-test，是唯一沒被證明冗餘的正交籌碼軸，走 FinMind）。
其餘不是冗餘、就是黑箱、就是無歷史/無公布日的快照陷阱。**macro 的最大缺口 M1B−M2 玩股網根本沒有，別在它身上找。**

## 9. 兩位新作者裁決（延伸 §4）

第一輪的 4+13 名單外，今天精選頁高頻出現兩位：

| 作者 | ID | 裁決 | 為什麼 |
|---|---|---|---|
| **玩股X檔案** | 203664 | **每日必看（升入白名單）** | 玩股編輯帳號、`promotion`=null 無社團牆。方法**完全可複現**：分點前35大主力買賣超比值 >1.2 偏多 / <0.8 偏空，且給**帶日期、可隔日對帳**的明牌（南亞 7/29 比值1.22→7/30 +2.47%；國巨0.78→跌停；聯電1.06→+7.32%）。**關鍵**：這套比值能用我們自己的 2024-07+ 分點 tape **獨立重算**，是少數「可在自家 DB 稽核作者是否誠實報自己數字」的作者。弱點：範例事後挑選、無 OOS |
| **KuraHoshi** | 23864 | **看情況（不進每日）** | `promotion`=null 但貼文自曝有 2022 起的**付費社團**（行銷管線存在）。指數級明牌可計分（「八月底前回測40000以下」、支撐35000-37000/41000-42000），但框架自相矛盾（「不會很久，但也會很久」）、無個股、少可證偽 call。只把他的**指數 call 丟記分板**追蹤即可 |

→ **新白名單（每日精選過濾用）**：`memberId ∈ {407988 華安/茲安, 203663 摸金, 343059 講客人, 98845 小博士, 144623 特派員, 203664 X檔案}`。144623 之外新增 203664。

## 10. 部落客「預測記分板」— 具體 schema（把「誰有 edge」變成可算的）

第一輪 §4 提過「假說記分板」概念，這裡給**可直接建表的設計**（append-only = 反倖存者偏誤的核心）：

**表 `author_predictions`（一旦寫入永不改/刪）**：
`member_id, author_name, post_id, made_date(T0), asset_type(stock|index), symbol, direction(up|down|range), horizon_days(1/5/20/60), check_date, condition(文字觸發), target_level, claim_text(逐字), confidence(從語氣編碼), entry_ref_price(made_date收盤，抽取當下凍結), status, outcome, realized_return, benchmark_return(同期TAIEX), alpha(=realized−benchmark), touched_target, scored_at`

**每日兩支 job**：
1. **早盤抽取**：撈精選頁+白名單新文 → LLM 抽 0..N 列（非預測文如 X檔案的新制報導 → **0 列**，記「見文無可計分」以量測 specificity）→ **立刻凍結 entry_ref_price**（殺前視）→ status=open
2. **盤後計分**：check_date≤today 的 open 列 → 讀 kbar 收盤+盤中高低 → 算 alpha / touched_target。**方向股 call：sign(alpha)==direction 才算 hit（benchmark 調整後，"只是喊對多頭"得 0 分）**；指數區間 call：TAIEX 是否落在帶內

**排行 `author_scorecard`（作者×horizon×資產）**：`n_calls, hit_rate, mean_alpha, hit_rate_pvalue(二項 vs 0.5), alpha_tstat, specificity(=可計分列/見文數), decay曲線`。
**護欄**：append-only+凍結入場價（無事後挑選）、benchmark 相對 alpha（區分 skill vs beta）、min n≥20 才排名、股/指數分兩榜、最終按風險調整 alpha 且過顯著性才排。
**加碼**：玩股X檔案的分點比值可用我們自家 tape **雙重計分**（他報的數 vs 我們重算）→ 順便驗他誠不誠實。
一年後這張榜比訂閱數可靠得多，直接回答「哪位部落格有用、哪位專欄沒用」。
