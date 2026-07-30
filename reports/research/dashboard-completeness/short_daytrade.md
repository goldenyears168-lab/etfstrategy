# 信用交易 × 微結構籌碼三訊號:借券賣出 / 當沖比重 / 融資使用率

研究日期 2026-07-30 ｜ 維度定位 L2 籌碼核心 ~ L3 微結構 ｜ 對照 champion = 外資台指期 positioning
（`fut_foreign_oi` z60>0, chip×bull OOS +1.79, 領先）

---

## 1. 這維度是什麼、專業為何看它

三個「信用槓桿 × 散戶/機構部位」代理，補進觀盤系統的空方與過熱視角：

| 訊號 | 白話 | 誰在動 | 專業看點 |
|---|---|---|---|
| **借券賣出 SBL** | 機構借股票賣出、尚未回補的空方部位 | 機構（知情空方） | 唯一有跨斷面學術背書的「知情做空」訊號；補 champion 缺的**現貨空方**維度 |
| **當沖比重** | 當日沖銷量佔總成交量% | 散戶（注意力/週轉） | 過熱/情緒代理；驟升＝短線過熱前兆 |
| **融資使用率** | 融資餘額 ÷ 融資限額 | 散戶（槓桿） | 極高＝追價籌碼耗盡，轉折區 contrarian 前兆 |

專業上看它們，不是拿來單獨擇時，而是當 **regime 內的濾網/部位調整**：多頭裡 SBL 空單堆積＋當沖過熱＋融資耗盡＝上檔動能將盡的組合訊號。三者資訊都偏「部位」而非「價格預測」，本質是動能的鏡像。

### 學術 / GitHub 依據

- **Chang & Chen (2009), *Short sale and stock returns: Evidence from TWSE*** — 台股借券賣出與後續報酬顯著**負相關**，做空者多為機構知情方 → 支持 SBL 空單當橫斷面**領先 bearish** 因子。
- **TWSE SBL 實證（sbls_v = 當日借券賣出額/成交額）** — 對未來報酬顯著負向；但借券方偏 FTSE 台灣中型100 成分 → **選樣偏誤**需注意。
- **Barber, Lee, Liu & Odean, *The Cross-Section of Speculator Skill: Evidence from Day Trading*（Berkeley, 台股資料）** — 台股當沖 <1% 持續獲利、80% 稅前虧損 → 當沖比重是散戶注意力代理，整體**負向/情緒**訊號，非 alpha。
- **Rev. of Asset Pricing Studies 13(4) 2023, *Short Interest and Aggregate Stock Returns*** — 總體短單餘額對大盤有（負向）預測力但屬低頻/總量層級 → SBL **大盤版偏同步~落後**，個股橫斷面才較領先。
- GitHub 參考：`FinMind/FinMind`（三個 dataset 直接支撐 scaffold）、`Andy-Liu66/Chip-Project`（margin/borrow 解析邏輯）、`symbiosis11503/invest-system`（TWSE OpenAPI 籌碼因子）、`kevin801221/stock-strategies-only`（籌碼因子正規化 + regime gating）。

---

## 2. 訊號精確定義（公式 / 正規化 / 方向）與資料源

### 2.1 借券賣出 SBL — 領先（bearish, 橫斷面）
- **公式**：`sbls_v = 當日借券賣出金額 / 當日成交金額`；或 `SIR = SBL賣出餘額 / 流通股數`。
- **口徑（關鍵）**：只用**已賣出且未回補**部位 = `SBLShortSalesCurrentDayBalance`。**不是**總借券餘額。
- **正規化**：rolling z60 或橫斷面 decile；總量版用成交值/市值正規化。
- **方向**：**負**（高 → 未來報酬低）。
- **時間窗**：T 日餘額約 21:00 公布 → 只可 **T+1** 使用（避免前視）。
- **資料源**：`FinMind TaiwanDailyShortSaleBalances`（信用額度總量管制餘額表, 2005-07 起, by `data_id`）。官方備援 TWSE 借券賣出 rwd endpoint（TWT93U 系）。
- **陷阱**：本地 `stock_lending_daily` 是**總借券餘額**（含避險/指數套利/CB 對沖/除息借券），**方向中性 ≠ 借券賣出**。用它當空方訊號會被噪音稀釋甚至反向。

### 2.2 當沖比重 — 同步 / L3 濾網
- **公式**：`ratio = 當沖量 / 總量`（或金額版）。
- **正規化（強制）**：rolling percentile / z。**2017-04 現股當沖稅減半（0.3%→0.15%）造成結構性跳升**，原始 level 非平穩，直接用 = 前視 regime。
- **方向**：**負**（過熱/週轉率過熱代理）。
- **資料源**：`FinMind TaiwanStockDayTrading`（2014-01 起）＋ `TaiwanStockPrice` 當分母。官方備援 TWSE BFIAMU（市場當沖比重）。
- **陷阱**：分母口徑（量 vs 值）結果不同；資券當沖互抵重複計數；`BuyAfterSale` 盤前名單只表「可當沖資格」非實際活躍度，別誤當活動量。

### 2.3 融資使用率 — 落後 / 極端 contrarian 前兆
- **公式**：`util = 融資餘額 / 融資限額`（個股或市場 Σ/Σ）。
- **正規化**：z60 或百分位；極端百分位當 contrarian gate。
- **方向**：極高 = 反市場（散戶追價耗盡→反轉前兆），**但只在特定 regime**；強多頭時高 util 會持續，勿逆勢。
- **資料源**：`FinMind TaiwanStockMarginPurchaseShortSale`（2001 起，含 `MarginPurchaseTodayBalance` 與 `MarginPurchaseLimit`）。官方對應 TWSE MI_MARGN（selectType=MS 信用交易統計）。
- **陷阱**：限額由券商按股本/波動調整 → 使用率有**機械式跳動非情緒訊號**；本地無 limit 欄，`stock_margin_daily` 只有 balance。

FinMind base = `https://api.finmindtrade.com/api/v4/data`（見 `.cursor/rules/finmind.mdc`）。

---

## 3. 本地資料可得性（誠實判定）

先實跑查了 `data/stocks.db` 與 `panel.parquet`：

| 訊號 | 本地表 | 狀態 | 判定 |
|---|---|---|---|
| 借券賣出 SBL | `stock_lending_daily`（170 檔, 2015→2026-07-08） | 只有**總借券餘額**（方向中性），**無** SBL 已賣出口徑 | **需接 FinMind** |
| 當沖比重 | `stock_daytrade_daily`（171 檔） | `daytrade_volume` **100% NULL**；`daytrade_ratio_pct` 僅 **13 個交易日**有值（1,620 列） | **實質空表 → 需接 FinMind** |
| 融資使用率 | `stock_margin_daily`（180 檔, 2015→2026-07-29） | 有 balance/change，**無融資限額欄** → 使用率算不出 | **需接 FinMind（補 limit）** |
| （同家族 proxy）市場券資比 | `panel.parquet` `short_bal`/`margin_bal`（2018→2026-07-29） | 市場層 `short_bal / margin_bal` 可直接算（無 limit，仍算得券資比） | **本地可跑** |

**結論：三個 headline 訊號本地皆不可算（`implementable_now = false`）。** 唯一 local 可跑的是市場層**券資比**（融券/融資，散戶口徑），與 SBL（機構口徑）不同，僅同家族 proxy——用它給本報告錨定真實數字。

---

## 4. 研究設計（依專案凍結門檻紀律）

1. **方向固定**：由 IN-SAMPLE IC 符號決定，不偷看 OOS。
2. **IS/OOS 分割**：時間序 70/30。
3. **permutation**：vs 同曝險隨機（固定 #long-days，洗哪幾天）→ 排除「剛好空在弱股/追多強股」。
4. **stage-matched permutation**（個股層 SBL 必做）：在同 Weinstein stage 內洗，否則只是動能重述。
5. **Deflated-Sharpe**：多重檢定校正（本家族預期不過）。
6. **regime-conditioning**：多頭（`ix_close > MA200`）與空頭分開看；預期 edge 僅多頭出現。
7. **corr_vs_champion**：與 champion 日報酬相關，判斷是否只是 champion/動能重述。
8. **不網格搜尋**：一次性誠實 OOS（呼應 980T/adopted-44 過擬合史）。

### 4.1 anchor 實測結果（市場券資比 proxy, 已實跑）

腳本 `scripts/research/dashboard/short_daytrade_study.py` 實跑 panel（n=1986, IS=1390 2018-06→2024-02, OOS=596 2024-02→2026-07）。基準：**B&H OOS Sharpe +0.39；champion OOS +1.12**。

| 訊號 | dir | IC_IS | OOS Sharpe | OOS perm_p | corr_vs_champ | OOS 多頭 | OOS 空頭 | 曝險 |
|---|---|---|---|---|---|---|---|---|
| 券資比 z60 `short/margin` | −1 | −0.017 | **+0.27** | **0.48** | +0.47 | +0.75 | −2.14 | 48% |
| 融券水位 z60 `short_bal` | −1 | −0.017 | −0.09 | 0.76 | +0.48 | +0.33 | −2.15 | 46% |
| 融資餘額 z60 `margin_bal` | +1 | +0.020 | +0.98 | 0.03 | +0.24 | +1.00 | +2.91 | 56% |

**判讀（誠實）**：
- **券資比**：OOS Sharpe 只 +0.27（<B&H+champion 組合價值），**permutation p=0.48 = 完全贏不過同曝險隨機**；且高度 regime 依賴（多頭 +0.75 / 空頭 −2.14）。→ 弱濾網，非獨立 alpha，且空頭反傷。
- **融券水位單獨**：OOS 負、perm p=0.76 → 確認研究結論「融券單獨無效」。
- **融資餘額 z60**：OOS +0.98、perm p=0.03 看似有效，但這正是 Stage-4 已知的**動能偽裝**（與價格動能相關 ~0.69，walk-forward 掉到 +0.29）；此處空頭仍 +2.91 是因訊號＝順勢，屬既有落後因子，非本研究新增。

anchor 明確支持全維度假設：**信用擁擠家族在市場層是弱的、regime 依賴的濾網，不是 standalone alpha。**

---

## 5. lead/lag 定位 × L0–L3 分層 × 與 champion 搭配

| 訊號 | lead/lag | 層 | 與 champion（外資期貨 positioning, 領先）怎麼搭 |
|---|---|---|---|
| **借券賣出 SBL** | **領先（bearish, 橫斷面）**；大盤總量偏同步~落後 | L2 籌碼核心 | **互補的空方維度**：champion 走期貨多空 positioning，SBL 走個股知情做空。多頭 champion 亮燈時，個股 SBL 高 → **過濾**掉被機構做空的個股；三者中最可能貢獻「與 champion 弱相關」的增量。 |
| **當沖比重** | **同步**（與當日波動）；驟升＝過熱**前兆** | L3 微結構 | **入場/風險濾網**：champion 給方向，當沖過熱百分位高時縮部位或延後進場。與價格/量能共線最重，證偽門檻最高，不當 regime。 |
| **融資使用率** | **落後**；極端高＝反轉**前兆** | L2（與既有 margin 弱落後因子同格） | **contrarian gate**：champion 多頭 + 融資使用率極端百分位 → 警示追價耗盡，僅多頭轉折區有效，不逆勢。 |

**優先價值排序**：借券賣出（補 champion 缺的做空維度）＞ 融資使用率（極端 contrarian gate）＞ 當沖比重（L3 濾網）。三者都應以「regime 內濾網/部位調整」進系統，先凍結門檻、一次性誠實 OOS。

---

## 6. 已知陷阱與規避

| 陷阱 | 規避 |
|---|---|
| **借券餘額 ≠ 借券賣出**（本地 `stock_lending_daily` 方向中性含避險套利） | 只用 `SBLShortSalesCurrentDayBalance`（已賣出未回補），不用總餘額 |
| **當沖 2017-04 稅改結構斷點**（level 翻倍非平穩） | 強制 rolling z / percentile，禁用原始 level |
| **融資使用率限額機械跳動**（券商調限額） | 標記限額調整日；用 z60/百分位而非絕對值 |
| **共通：籌碼是動能偽裝**（三者與價格動能高度共線） | permutation vs 同曝險 + stage-matched perm + corr_vs_champion，全部過關才採信 |
| **共通：僅多頭有效** | regime-conditioning（`ix_close>MA200`）分開驗，空頭不用 |
| **共通：盤後公布延遲**（15:00~21:30） | 回測 shift 到 T+1，禁用當日收盤同步 |
| **SBL 選樣偏誤**（借券偏中型100 成分）＋軋空回補反向急拉 | 橫斷面 decile 內比較；標記 covering 事件 |
| **本地個股表覆蓋不足**（margin 180 檔、daytrade 空表） | 做個股研究前先用 FinMind 補全 universe + 日期 |

---

## 7. 下一步（scaffold 已備）

`scripts/research/dashboard/short_daytrade_study.py` 已含三個 FinMind 抓取函式（`fetch_sbl_shortsale` / `fetch_daytrade` / `fetch_margin_with_limit`）＋ `build_signal_from_external` 評估接口，import 安全、呼叫才抓。啟用步驟：

1. `export FINMIND_TOKEN=...`
2. 抓 SBL（`TaiwanDailyShortSaleBalances`）→ 建市場層 sbls_v，接 anchor 式 IS/OOS + perm。
3. 抓當沖（`TaiwanStockDayTrading` + `TaiwanStockPrice`）→ rolling-normalize 後同框架驗。
4. 抓 margin+limit（`TaiwanStockMarginPurchaseShortSale`）→ 算市場 Σbalance/Σlimit 使用率。
5. 個股層 SBL 加 stage-matched permutation（`src/stage_analysis.py`）。

建議另建 sync：`sync_sbl_shortsale_daily.py`、`sync_daytrade_ratio_daily.py`，並 backfill `MarginPurchaseLimit` 進 `stock_margin_daily` 以啟用融資使用率。

---

## 8. Phase-2 執行結果與收尾（2026-07-30）

**anchor(市場券資比 proxy)已實跑,乾淨結果**(`short_daytrade_anchor.csv`,panel 2018→2026,OOS n=596):

| 訊號 | dir | OOS Sharpe | OOS perm p | corr_vs_champ | bull regime | 判讀 |
|---|---|---|---|---|---|---|
| 券資比 z60 | −1 | +0.265 | **0.476** | 0.465 | +0.75 | 贏不過同曝險隨機 → 弱濾網 |
| 融券水位 z60 | −1 | −0.090 | 0.761 | 0.480 | +0.33 | 失敗 |
| 融資餘額 z60(對照) | +1 | +0.982 | 0.027 | 0.243 | +1.00 | 動能代理(已知) |

→ 本地可算的「信用擁擠」proxy = **弱濾網,非獨立 alpha**,與既有融資研究一致。

**headline 三訊號(借券 SBL / 當沖 / 融資使用率)未完成**:Phase-2 fetch agent 僅抓到 **1 列**有效資料(`short_daytrade_data.parquet` 的 `sbl_short_bal`/`daytrade_val` 非空僅 1 筆)。原因:`TaiwanDailyShortSaleBalances`、`TaiwanStockDayTrading` 是**逐股 dataset**,單次市場層呼叫抓不到日序列,需**全宇宙逐股抓取後 per-date 聚合**(吃配額),或改用 **TWSE 市場層 endpoint**(當沖統計 `TWTBAU`、借券賣出彙總)。→ 已列入 **Phase-3 backlog(最高優先 #9)**,以 TWSE 市場層正式回填後重跑本框架。

**維度總結 verdict**:`券資比 proxy = 弱濾網(perm 0.48)`;headline SBL(唯一有學術背書的知情空方)待正式回填才能定論,但家族先驗偏「動能鏡像/regime 濾網」而非獨立 alpha。落 L2~L3,與 champion 搭配為過熱/擁擠確認,非 alpha 疊加。

---

*本報告為研究方法論，非投資建議。所有訊號經證偽式驗證前不得用於實際交易決策。*
