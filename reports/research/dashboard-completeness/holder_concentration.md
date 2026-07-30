# 大戶持股集中度(千張大戶 / 集保戶數 / 董監質押)— Holder Concentration

維度層級:**L2 籌碼核心(個股橫斷面)+ 質押 tail-veto**(L0 regime → L1 價量 → **L2 籌碼核心** → L3 微結構)
分類定位:**同步偏落後 / 前兆(個股層),非領先;個股橫斷面選股+風控,不是大盤擇時**
狀態:**implementable_now = True(集保集中度子訊號已用真資料實跑,結論為乾淨負面);質押子訊號待補(FinMind 無此集)**
verdict:**動能偽裝 / 無效(null)**——嚴格千張大戶Δ、千張z、合成集中因子在 164 檔大型股、183 週真資料上**皆無可辨識的橫斷面前瞻 edge**(IC 近零、OOS 翻號、permutation p=0.26–0.77 遠不顯著、Deflated-Sharpe 全 fail、去動能後翻負);與價格動能 corr≈0.21–0.23,其唯一與報酬的微弱共動來自動能。

研究腳本:`scripts/research/dashboard/holder_concentration_study.py`(REAL DATA 可實跑,已移除集保 scaffold)
資料現況(REAL,已落地):
- `data/research/dashboard/holder_concentration_data.parquet` — FinMind `TaiwanStockHoldingSharesPer`,**164 檔大型股**(`stock_market_value_daily` 宇宙),週頻,**2023-01-07→2026-07-24(183 週)**,逐級距 people+percent,506,702 列。
- `data/stocks.db` `stock_close_adjusted`(adj_close_v2 還原股價 → forward return/動能)、`stock_shareholding_daily`(foreign_remaining_ratio → 外資託管汙染 control)
- `data/research/chip_macro/panel.parquet`(ix_close→regime;fut_foreign_oi→champion 共線檢定)

---

## 1. 這維度是什麼、專業為何看它

「誰持有、持有得多集中、內部人有沒有把股票押去借錢」是價量與法人買賣之外的第三條足跡——**股權結構**。三個子訊號合成一格:

1. **千張大戶持股比 `big_pct`**:TDCC 集保「>1,000 張(>1,000,000 股)」級距的持股占比。**集中度水位**:大戶(法人/主力/內部人)積不積籌。
2. **集保戶數 `holders`**:各級距人數(people)合計 = 持有人數 = **breadth of ownership**。散戶進出的廣度代理。
3. **董監質押比 `pledge_ratio`**:董監設質股數 ÷ 董監持股。**下檔脆弱度狀態變數**——內部人把股票押去借錢,股價逼近斷頭價時會誘發被迫賣壓 cascade。

專業看它的核心理由:**張數 vs 人數的張力**。在地籌碼派(wistock/CMoney/股感)標準做法是「千張大戶 × 集保戶數」2×2 象限——**張數↑ 同時 人數↓ = 集中(最強偏多)**,籌碼被少數大戶鎖住;**張數↓ 人數↑ = 鬆動(偏空)**,籌碼分散給散戶。質押則是**獨立的風控維度**,不是拿來做多空,而是當價格逼近斷頭時的 risk-off 燈號。

### 學術 / GitHub 依據
- **Chen, Hong & Stein (2002), *Breadth of Ownership and Stock Returns*, JFE / SSRN 262106** — 本題操作化正典。以 Δbreadth(持有人數/機構數變動)做橫斷面 decile,最低變動 decile 未來 12M 顯著跑輸最高 ~6.4%/yr(放空受限+隱藏壞消息假說)。**關鍵反向張力**:CHS 說 breadth 下降預示報酬「低」,與台股散戶派「人數減少=集中=看漲」方向相反 → 必須把 `big_pct` 與 `holders` 拆開,不可只用單一 `holders`。
- **Choi, Jin & Yan (2013), *What Does Stock Ownership Breadth Measure?*, NBER w16591 / Review of Finance** — 區分 retail vs institutional breadth;retail breadth change 的預測力在大型股較強且隨時間衰減 → 因子有 regime/時代依賴,呼應本專案「edge 會衰減」的鐵律。
- **Chan/Chen/Hu/Liu, *Share Pledges and Margin-Call Pressure*;Wang, *Causes & Consequences of Stock Pledging: the Case of Taiwan*** — 控制股東質押接近斷頭時有掏空與被迫賣壓誘因、宣告報酬較低、崩跌風險↑ → 質押應做成**非線性 tail 狀態變數/風控 veto**,非線性 alpha。台灣 2011 起強制揭露,資料可得。
- **Yan (2026), *Multiple Large Shareholders and Controlling Shareholders' Equity Pledging*, IJFE** — 多大股東制衡抑制質押的負面效果 → 質押因子應與股權集中度**交互**,支持把「集中度」與「質押」合看。
- **GitHub / 資料層**:`FinMind/FinMind`(`TaiwanStockHoldingSharesPer` API 與欄位定義,付費級);`voidful/tw_stocker`(每日爬存落地框架,可仿其結構自建 TDCC 週度歷史庫,解「官網只給近期」陷阱);FinLab `python_crawler_tdcc` 教學(公開 TDCC 集保股權分散表爬蟲)。**未見任何公開 repo 對此三訊號做過 IS/OOS+permutation+DSR 嚴謹驗證** → 與 chip-macro 同況,自建。

---

## 2. 訊號精確定義(公式 / 正規化 / 方向)

集保表每週五盤後~週六公布(資料截至該週最後交易日),質押 MOPS 每月 10 日前申報。**一律低頻**。

| 子訊號 | 原始量 | 訊號(正規化) | 方向(由 IS IC 定,無 OOS peek) |
|---|---|---|---|
| 集中度水位 | `big_pct` = level「>1,000,000 股」的 percent | `Δbig_pct` 週變動,取 trailing 26 週 z-score `z_big` | 集中→偏多:`z_big > 0` |
| breadth | `holders` = Σ people(各級距人數) | `Δholders` 週變動 z-score `z_holders` | 人數↓→偏多(但須與 z_big 同時,見張力) |
| **合成集中因子** | — | `concentration = z_big − z_holders` | `>0` = 張數集中且人數收斂 = 偏多 |
| 質押 tail | `pledge_ratio` = 設質股數/董監持股 | `high_pledge = pledge_ratio > 0.4` **AND** 股價自 52w 高回落 > 15% | **veto(risk-off)**,非做多因子 |

**方向張力(CHS 陷阱)**:偏多的**必要條件是「大戶張數↑ 同時 人數↓」**;若只有 `holders↓` 而 `big_pct` 未增(retail capitulation),反而落入 CHS 的負向 breadth。故用合成 `z_big − z_holders`,不用單分量。

**正規化**:trailing 12–26 週 z-score,或橫斷面 decile rank(跨股同日排序,更接近 CHS 學術做法且自動去市場 beta)。在地建議至少「連 3–4 週同號趨勢」去單週雜訊。

**質押是非線性 tail 變數,不做線性 alpha**:只有高質押 AND 逼近斷頭區才有含意 → 做成 conditioning filter / veto,不當獨立多空(見 `pledge_tail_veto()`)。

**無前視(point-in-time)——本維度頭號技術風險**:集保資料最壞 stale 達 5 交易日 → 對齊務必用**公布日**而非資料日,否則免費 look-ahead。腳本 `build_panel()` 以「資料日 + publish_lag(4 日)後第一個交易日」為 entry。質押以「申報公布日(次月 10 日前)」對齊。

### 資料源(REAL,已落地 + 剩餘 backfill)
- **集保(千張大戶 + 集保戶數)= 已抓真資料**:FinMind `TaiwanStockHoldingSharesPer`(date/stock_id/HoldingSharesLevel/**people**/percent/unit),已抓 **164 檔大型股、183 週(2023-01-07→2026-07-24)**,存 `data/research/dashboard/holder_concentration_data.parquet`(506,702 列,0 失敗)。**已用嚴格千張(`more than 1,000,001`=>1,000 張)口徑,並保留 people 組集保戶數/breadth**,不再是舊 cache 的寬口徑 ≥100 張 proxy。
- **價格**:`stock_close_adjusted.adj_close_v2`(還原股價,2504 檔 2010→今)→ 修正專案級「股利未還原」盲點;`panel.parquet` ix_close → regime;`stock_shareholding_daily.foreign_remaining_ratio` → 外資汙染 control。
- **已抓範圍 vs 完整宇宙(誠實)**:僅 164 檔**大型股**(市值宇宙),非全上市~1,000 檔;時窗 2023→今(~3.5 年,週頻),非 2018→今。⚠️ 大型股正是「>1M 級距被外資保管行/ETF 汙染最重」的一段(本宇宙千張大戶比中位數 **59.5%**),真內部人/主力集中訊號在此段最被稀釋。**剩餘 backfill**:中小型股(千張大戶更接近真主力)+ 2018–2022 回補,尚未抓(配額/時間限縮)。
- **董監質押 = 待補**:MOPS `t93sb`(POST 表單,月頻),**FinMind 無此集**,本次未接;報告中質押 tail-veto 仍為設計,verdict 標「待補」。

---

## 3. 研究設計(依專案方法論)

1. **IS/OOS 分割**:時間序 70/30,方向只用 IS IC 符號固定,OOS 不 peek。
2. **Permutation**:因子為橫斷面 → **在每個 date 的橫斷面內打亂 signal**(保持同日同曝險/同截面結構),2000 次,取 `|null IC| ≥ |actual|` 比例為雙尾 p 值(`perm_ic_p()`)。
3. **Deflated-Sharpe**:因子若進到 portfolio 形式須對搜尋次數做 DSR 折減。**警語**:週頻 ~52 obs/yr、質押月頻 ~12 obs/yr → 檢定力先天低,頭條 Sharpe 極易被搜尋灌水(呼應 champion DSR borderline fail)。
4. **Regime-conditioning**:分多頭/空頭子樣本測(`_market_regime()`:IX0001 close>MA200 且 MA200 上彎)。集中度 edge 預期只在 Stage-2 上升段(大戶趁強積籌)成立,空頭失效。
5. **動能共線控制(頭號陷阱)**:算 partial IC——signal 與 fwd_ret 各自對 20 日動能取 rank 殘差後再相關(`partial_ic()`),避免只是動能偽裝。

### 正式研究實跑結果(2026-07-30,REAL DATA)

在**真資料(164 檔大型股、183 週、2023-01→2026-07,n=28,880 stock-week,平均 158.7 檔/週)**上跑 `run_study()`。IS/OOS = 70/30 時間切(IS 2023-01-30→2025-07-02,OOS 2025-07-09→2026-07-15,OOS 全程多頭)。方向由 IS IC 符號固定;permutation 每 date 橫斷面內打亂 2000 次;long-short = 每週 top/bottom 20% 分位等權,52 週年化;DSR 對 3 個試驗 + 偏態/峰度折減。

**champion 共線**:每週橫斷面平均合成集中因子 vs 外資台指期 OI 週變動,Spearman corr = **−0.075**(近零)→ 個股集中度與大盤 champion positioning **正交、不同層,互不替代**。

| 訊號 | IC_all | IC_IS→IC_OOS | 去動能 partial IC | 去動能+外資 partial IC | perm p(雙尾) | LS Sharpe(全/OOS) | DSR |
|---|---:|---:|---:|---:|---:|---:|---:|
| 千張大戶Δ `d_big` | −0.006 | −0.000 → −0.014 | **−0.008** | −0.006 | **0.588** | −0.07 / −0.04 | **0.16** |
| 千張大戶z `z_big` | −0.010 | −0.006 → −0.015 | **−0.012** | −0.011 | **0.262** | 0.09 / 0.85 | **0.25** |
| 合成集中 `z_big−z_holders` | −0.002 | +0.008 → **−0.016(翻號)** | −0.004 | −0.004 | **0.774** | 0.32 / **−0.97(翻號)** | **0.40** |
| (對照)動能 `mom20` | +0.010 | — | — | — | — | — | — |

輔助:corr(合成集中, mom20)=**+0.234**、corr(千張大戶Δ, mom20)=**+0.215**(訊號約 21–23% 與動能共線);IC_bull/IC_bear 三訊號皆在多頭段為負、空頭小樣本為正(與「大戶趁強積籌」的 regime 預期**相反**,屬雜訊翻轉)。

**誠實結論(證偽優先)**:嚴格千張大戶(不再是舊寬口徑 proxy)、保留 people 的合成集中因子、在**乾淨、良好檢定力的 164 檔×183 週真資料**上,**三個子訊號皆無可辨識的橫斷面前瞻 edge**:
- IC 一律近零(|IC|≤0.01),且 IS→OOS 不穩(合成因子甚至翻號);
- permutation p 落在 0.26–0.77,**遠不顯著**(需 <0.05);
- Deflated-Sharpe **全數 fail**(0.16–0.40,需 >0.95)——即便 z_big OOS Sharpe 0.85、合成因子全期 Sharpe 0.32,經搜尋+分布折減後皆非真 alpha;
- 去動能殘差化後 IC 皆為 **0 或負**,訊號與報酬僅有的微弱共動來自 21–23% 的動能共線 → **「動能偽裝」而非獨立籌碼 alpha**。

這比舊 46 檔示範版可信得多(口徑正確、宇宙×4、樣本×3、含 people 合成、還原股價、加外資汙染 control),結論一致且更硬。**覆蓋範圍限制(不誇大)**:本檢定僅涵蓋**大型股**——正是 >1M 級距被外資保管行/ETF 稀釋最重的一段(千張比中位數 59.5%),此段的 null **不能外推**到中小型股(千張大戶更接近真主力/內部人)。中小股 + 2018–2022 回補為明確 backfill 缺口。

---

## 4. lead_lag 定位 + 落層 + 與 champion 怎麼搭

對照 champion = **外資台指期 positioning(領先,日頻,大盤層)**:

| 子訊號 | lead/lag | 落層 | 說明 |
|---|---|---|---|
| 千張大戶 `big_pct` | **同步偏前兆(個股)、可交易時已同步-落後** | L2 個股籌碼 | 大戶積籌與股價大致同步、略前於延續段;週頻+5 日 stale 使可交易時接近同步。且被外資託管汙染 → 帶「外資現貨」同步性質。 |
| 集保戶數 `holders` | **落後-同步、反向前兆** | L2 個股籌碼 | 散戶人數變化多為反應式(追高殺低);人數暴增≈熱度頂、銳減≈恐慌底 → 當 contrarian 前兆讀。 |
| 董監質押 `pledge_ratio` | **落後(揭露面)、下檔尾部前兆(狀態面)** | L2/風控 veto | 月頻申報=落後;高質押=潛伏脆弱度,逼近斷頭價時轉為 cascade 前兆/加速器。 |

**與 champion 的搭配(實測後定調:正交但無獨立 edge → 只當 context/veto,不當選股因子)**:
- **層級不同、正交、不可替代**:實測合成集中因子(每週橫斷面均值)vs 外資台指期 OI 週變動 corr = **−0.075**,近零 → 兩者確為不同層、不共線。**千萬別把集中度加總做 TAIEX 擇時**(全市場加總=breadth 反指且被外資託管汙染)。
- **~~確認(confirmation)~~ — 實測不成立**:原設計「champion 綠燈日於集中度 top-decile 選股」在真資料上**沒有支撐**:集中因子 IC 近零、OOS 翻號、DSR fail、去動能翻負,在多頭段(理應最有效)IC 反為負。故**不建議**把它當多頭日的選股疊層。
- **前兆(precursor)**:集保戶數銳減理論上是個股層散戶投降前兆,但本檢定的 `holders` 變動分量並未展現獨立前瞻力(併入合成因子後仍 null)→ 保留為觀察量,非可交易前兆。
- **veto(過濾)= 唯一存活用途,但質押子訊號待補**:高質押 tail 燈號作為風控否決仍為合理設計,惟 MOPS 質押資料本次未接,verdict「待補」;集保集中度本身不建議進入 alpha 疊層,至多作為個股籌碼結構的**背景描述**。

---

## 5. 已知陷阱與規避

1. **保管銀行/借券/ETF 汙染(最大資料陷阱,實測應驗)**:>1,000 張級距混入外資保管銀行帳戶、ETF 申贖大宗、借券池——這些「假大戶」與真內部人集中無關。本宇宙(164 大型股)千張大戶比**中位數 59.5%**(min 15.5%、max 95.6%,TSMC 84.91%)→ 大型股此級距根本是外資託管+ETF 的鏡子,真主力訊號被稀釋殆盡,**很可能就是 null 結果的主因**。**規避**:已用 `foreign_remaining_ratio` 變動做 partial IC control(結論不變);更根本的規避是**改測中小型股**(千張大戶=真主力),列為 backfill 缺口。
2. **與價格動能共線(實測應驗)**:大戶比例上升常「因為」股價漲(贏家吸籌)。真資料 corr(訊號, mom20)=**+0.21~0.23**、去動能殘差化後 IC 歸零/翻負——**重演 Stage-4 融資教訓**,確定屬「動能偽裝」。**規避**:一律報 partial IC / 動能殘差化,不看原始 IC(腳本 `partial_ic()` 已內建 mom20+foreign 雙 control)。
3. **前視 / 週度錯位**:用資料日而非公布日對齊 = 免費 look-ahead;週/月頻樣本少 → DSR 極不友善。**規避**:公布日 lag(腳本已做);報告頭條 Sharpe 一律配 permutation + DSR 折減。
4. **方向張力未拆分量**:只用集保戶數↓當多頭會撞上 CHS 負向 breadth。**規避**:用合成 `z_big − z_holders`,要求大戶張數↑與人數↓同時成立。
5. **用錯層級**:個股橫斷面因子拿去做大盤 TAIEX 擇時無效。**規避**:只在個股橫斷面/選股用,不進大盤 timing。
6. **regime 依賴**:集中度多頭有效、空頭失效;質押只有接近斷頭價才有訊號。**規避**:regime-conditioning + 質押做非線性 tail 而非線性 alpha。
7. **regime 依賴 / 樣本先天短(部分應驗)**:集中度理論上多頭有效、空頭失效,但真資料多頭段 IC 反為負;週頻 ~52 obs/yr → DSR 先天不友善,本次 DSR 全 fail。**規避**:regime-conditioning(已做)+ 質押做非線性 tail;回補 2018–2022 拉長樣本。

---

## 6. 最終 verdict(2026-07-30,REAL DATA)

| 項目 | 結論 |
|---|---|
| **verdict** | **動能偽裝 / 無效(null)** — 集保集中度三子訊號在 164 大型股×183 週真資料上無獨立橫斷面 alpha;唯一與報酬的共動來自 21–23% 動能共線 |
| **落層** | **L2 個股籌碼核心**(非 L0 大盤、非領先);質押 tail-veto 屬 L2/風控,**待補** |
| **與 champion(fut_foreign_oi)** | **正交**(corr −0.075),不同層不可替代;但因無獨立 edge → **不進 alpha 疊層**,至多當個股籌碼結構背景 / 未來質押 veto |
| **可實作?** | 集保子訊號 `implementable_now=True` 但**結論為負**(乾淨證偽,不採用);質押子訊號 `待補`(FinMind 無、需 MOPS 自爬) |
| **覆蓋範圍(誠實)** | 已抓 = 164 檔**大型股** × 2023–2026(183 週);未抓 = 中小型股(千張=真主力,最可能翻案的一段)+ 2018–2022 + 董監質押。大型股 null **不外推**中小股 |
| **下一步(若續)** | 抓中小型股千張大戶(避外資託管稀釋)重測;接 MOPS 質押做 tail-veto;否則此格結論定為「證偽,dashboard 標灰」 |

---

*本報告為量化研究方法論記錄,非投資建議。作者非持牌投資顧問,任何訊號/因子僅供研究參考,不構成買賣要約。籌碼與股權結構資料有揭露時滯與口徑汙染,實作前務必自行驗證。*
