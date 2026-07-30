# 專家多維度組合 — 優先研究組合清單 vs 我們測過什麼

_研究員: 首席量化研究組 · 日期: 2026-07-31 · 4 份專家掃描(台灣籌碼實務 / SSRN學術 / 國際quant實務 / 六大經典型態)整併排序_
_對照基準: `STATE_OF_DASHBOARD.md`(Phase 1–6 收官)· `champion_conditional_gates.csv` · `l0xl1_combined_system.md` · `margin_maintenance.md` · `global_macro.md`(VIX gate)· `crash_thermometer`(證偽)· MEMORY 各 project 筆記。非投資建議。_

---

## 0. 一句話總結

**專家四路掃描與我們自證高度收斂到同一條主線:單維度幾乎沒有 alpha,反覆有效的只有「regime-conditioning(閘門)+ 第二維度確認」。** 我們已測掉的組合(champion×MA200、champion×VIX、L0×L1)恰好覆蓋了學術背書最硬的三塊(positioning×regime、動量×波動、factor-timing 被證偽)。**真正「專家高度推崇但我們還沒正式建模」的缺口只有三個:①外資現貨×期貨的 hedge-short 過濾(champion 目前是單腳)②P/C×VIX×breadth 恐慌 stack 的「多維同時對齊」版(我們只有 VIX 單閘)③short-interest × 機構持供給約束的雙排序(Asquith 2005,台股從沒人做)。** 其餘專家組合(吸貨結構、頂背離、軋空、期現背離判讀)在台股全是坊間經驗法則/事後挑例,多為「動能偽裝」——買上升趨勢包裝成籌碼技巧,我們已在 branch-follow 研究反覆證偽。

---

## 1. 專家優先組合排行(全表)

實證強度定義:**強**=有同儕審查 OOS/顯著性(或我方 DSR 過關)· **中**=有學術方向支持但樣本/落地保留 · **弱**=有邏輯但僅個案/小樣本/自證偽 · **僅坊間**=只有券商/內容平台經驗法則,無任何公開驗證。

| # | 組合 | 專家核心邏輯 | 實證強度 | 對應我們維度 | 我們測過沒 | 建議測試設計 |
|---|---|---|---|---|---|---|
| 1 | **外資期貨positioning × 波動regime(VIX)** | positioning 不預測短期,須疊 regime;動量在高波panic崩盤,dynamic momentum「Sharpe翻倍」 | **強**(Daniel-Moskowitz 2016 JFE;我方 DSR 0.998) | champion × VIX gate | **是** ✅ | 已部署。剩:換在地 VIXTWN 同日版重驗(理論更佳) |
| 2 | **外資期貨positioning × 趨勢regime(MA200)** | COT 須疊技術趨勢確認「最大參與者是否支持」;Weinstein Stage-2 | **強**(我方 OOS DSR 0.995) | champion × close>MA200上彎 | **是** ✅ | 已部署為系統C核心。剩:tracker caliber 對齊(現用MA150-stage) |
| 3 | **P/C × VIX × breadth × RSI 恐慌stack** | 四維同時對齊才是contrarian底;單一P/C只是一個資料點 | **中**(國際小樣本;台股自證偽) | options_micro + global_macro(VIX) + breadth + price_volume | **部分** ⚠️ | ★缺口:我們只有VIX單閘 + crash-thermo證偽。**沒測過「多維同時對齊觸發」的contrarian進場** |
| 4 | **外資現貨 × 外資期貨OI(hedge-short過濾)** | 期現同向確認趨勢;現賣+期多=避險非看空,過濾hedge雜訊 | **中**(方向邏輯硬,無OOS) | champion(期貨) + cashForeign(現貨) | **部分** ⚠️ | ★缺口:champion是**單腳**;cashForeign>0 單獨無效(G4 d=-0.41)。**沒測過現貨當期貨的「避險過濾器」交互** |
| 5 | **short interest × 機構持股(供給約束)** | 高short只在低機構持股(借券供給緊)時才預測負報酬;約95%股票約束不緊、交互無效 | **強**(Asquith-Pathak-Ritter 2005 JFE) | 融資維持率/券資比 × 大戶持股(holder_conc) | **否** ❌ | ★缺口:台股從沒人複製此雙排序。券資比×千張大戶 double-sort,只在「緊約束」子集看負報酬 |
| 6 | **維持率斷頭底 × 外資doi回補** | 融資投降(維持率<130-140)+法人回補=浮額洗清底 | **弱**(我方 perm p=0.007 但 n=4 極小) | margin_maintenance × futures(doi) | **是** ✅ | 已接 daily_tracker 雙確認。方向真、樣本弱,無法再硬化 |
| 7 | **factor估值價差 × 因子擇時(value-spread timing)** | spread寬→加碼因子 | **強(被證偽)**(Asness-Ilmanen 2017 JPM) | rev_yoy_3m 動態擇時 | **是** ✅ | 已避開:Phase-5 把 rev_yoy 弱化為長期tilt非擇時,與學界一致 |
| 8 | **機構order flow × horizon** | 法人流向>30min後不可預測;季頻機構流入負向、賣出更強(非對稱) | **中**(Campbell 2005;arXiv 2508.06788) | 三大法人買賣超 | **是** ✅ | cashForeign 單獨已證偽(G4/G4b d<0);horizon保留有限 |
| 9 | **分點主力 × 散戶(融資)背離 × 量價** | 法人買+主力買+散戶賣+量增價穩「四同向」吸貨 | **弱→動能偽裝** | branch + 融資 + price_volume | **是** ✅ | branch-follow 已證偽:seat「技巧」=買上升趨勢(stage-matched perm p=0.15-0.23) |
| 10 | **外資 × 投信同買(土洋合作)× 均線題材** | 兩者同買=基本面+題材雙信心;投信認養5日勝率75%(自述) | **僅坊間** | 三大法人 + tech_regime | **部分** | G5 trustStreak≥2~5 已測:d全為負(-0.18~-0.33)。同買gate無增益 |
| 11 | **券資比 × 基本面轉機 × 融券方向拆解** | 真軋空須基本面+法人增持+融券增快於融資;拆真空單vs套利空單 | **僅坊間**(個案挑例) | 融資/融券 + rev_yoy + holder_conc | **否** | 低優先:台股無軋空因子文獻,依賴事後挑例 |
| 12 | **價創新高 × 量縮 × RSI/MACD頂背離 × 主力出貨** | 頂背離=動能衰竭 | **弱→敘事**(多空皆可事後解釋) | price_volume + options | **是** ✅(概念) | 背離型態=過度配適溫床,不建議當可回測alpha |
| 13 | **集保大戶(400/千張)升 × 散戶降 × 股東數減 × 法人買** | 籌碼集中=起漲領先 | **僅坊間** | holder_concentration + 三大法人 | **是** ✅ | holder_concentration 已證偽(全null) |
| 14 | **Zweig breadth thrust** | 10日EMA廣度<40→>61.5% 急升=6-12月大漲 | **弱**(樣本~20次,rarity嚴重) | breadth | **是** ✅ | breadth 已證偽;thrust=稀有regime觸發器非alpha |
| 15 | **阿斯匹靈多空寶(月季線+期貨淨+選擇權約當大台+借券)** | 最系統化的多維大盤擇時商業框架 | **僅坊間**(付費品無獨立驗證) | champion + options + 借券 + tech | **部分** | 各腳我們單獨測過多為null;整合框架未原樣複製,但成分已知弱 |

---

## 2. ★ 專家高度推崇「但我們還沒測」的缺口(= 下一步最該做)

只有 **3 個** 真缺口通過雙重篩選(專家推崇 **且** 我們沒正式建模 **且** 有超過坊間的背書):

### 缺口 A — 外資現貨 × 期貨的 hedge-short 過濾(最高優先)
- **為何**: champion 目前是**單腳**(只用期貨 OI positioning)。四路專家中三路獨立指出「期現交叉可過濾避險空單雜訊」——現貨賣+期貨淨多=避險而非看空,把它讀成看空是 hedge-short 陷阱。這是把我們最強 factor 加一個**正交確認腳**的最自然升級,且成本為零(現貨資料已在 panel)。
- **背書等級**: 中(方向邏輯硬,台股實務共識,但無 OOS)。cashForeign 單獨在 G4 是負的(d=-0.41),所以這**必須是交互/過濾**而非相加——正好是「second-dimension confirmation」範式。

### 缺口 B — P/C × VIX × breadth 的「多維同時對齊」恐慌 stack
- **為何**: 我們有 VIX 單閘(DSR 0.998),但**從沒測過「P/C高 + VIX飆 + breadth極度oversold 三者同時觸發」的contrarian進場**。crash-thermometer 證偽的是**單一溫度計對 fresh event 的判別**(31-35%),不是「多維同時對齊」的稀有底部訊號——這是不同假設。Daniel-Moskowitz 的 panic-state 邏輯支持它。
- **背書等級**: 中(國際小樣本 + 我方VIX單腳已過)。誠實保留:台股罕見事件、樣本極少,大概率只能當 regime 觸發器非高頻 alpha。

### 缺口 C — short interest × 機構持股供給約束雙排序(Asquith 2005 台股複製)
- **為何**: 這是**學術背書最硬**的「交互勝單一」經典(JFE 2005),而**台股從來沒人做**。券資比/融資維持率當放空需求 proxy × 千張大戶持股當借券供給 proxy,只在「低機構持股 + 高券資比」的緊約束子集才預期顯著負報酬。是本專案 holder_concentration + margin 兩個已證偽單維的**交互復活**機會。
- **背書等級**: 強(頂級期刊),但誠實:原論文顯示約 95% 股票約束不緊、交互只對一小撮有效——台股散戶主導、融資≠美式 short interest,mapping 不完美,預期命中面很窄。

**其餘全部專家組合皆已測過或屬坊間/動能偽裝,不構成新缺口。**

---

## 3. 誠實區分:有學術/實證背書 vs 只有坊間說法(常是動能偽裝)

### 有硬背書(同儕審查或我方 DSR 過關)—— 值得建模
| 組合 | 背書 | 我方對應 |
|---|---|---|
| positioning × 波動regime | Daniel-Moskowitz 2016 JFE「dynamic momentum Sharpe翻倍」 | ✅ VIX gate DSR 0.998 |
| 訊號 × 趨勢regime gating | 跨市場 HMM/regime-filter 共識 | ✅ MA200 DSR 0.995 |
| short interest × 機構持股 | Asquith-Pathak-Ritter 2005 JFE | ❌ 缺口C |
| options order flow × 現貨 | Pan-Poteshman 2006 RFS(短天期≤1週) | options_micro 已測null(台股散戶PCR雜訊高) |
| 機構flow × horizon/macro-news | Campbell 2005;arXiv 2508.06788 | ✅ cashForeign 已證偽 |
| factor估值價差擇時 = **弱/被證偽** | Asness-Ilmanen 2017 JPM | ✅ 我們已避開(rev當tilt非擇時) |
| 台股法人herding × 市場狀態 | tandfonline 2025;Springer 2011 | 部分(regime-conditioning已內化) |

### 只有坊間說法 —— 幾乎全是「動能偽裝」,當敘事/風控濾網,勿當獨立 alpha
- **分點主力吸貨結構「四同向」**:我方 branch-follow 已拆穿——seat「技巧」= 買上升趨勢,扣掉個股 Weinstein Stage-2+RS 後 perm p=0.15-0.23,**edge 是個股趨勢在做功,不是籌碼**。這是最典型的動能偽裝。
- **頂/底背離型態**:同一型態多空皆可事後解釋(縮量創高=洗盤 or 出貨?),事後選擇偏誤高,過度配適溫床。
- **期現背離判讀「萬口」門檻**:零售啟發式,無公開 OOS;我方 champion 研究顯示裸 OI 單獨 DSR fail。
- **軋空券資比、集保大戶集中、投信認養75%勝率**:全 case-study/自述,無獨立驗證;holder_concentration 與 breadth 我方單維已證偽。
- **阿斯匹靈多空寶**:付費商業品,成分腳我方單獨多測為 null。

**共同教訓(專家與我方完全吻合)**:台灣籌碼實務「幾乎沒有單一維度能用、必須交叉組合」的口號**方向對**,但他們的「交叉」是**敘事式相加**;真正耐久的是**gating(閘門)/條件化**,不是線性疊加。凡宣稱「主力/分點技巧」的組合,先扣掉個股自身趨勢再看是否還剩東西——通常不剩。

---

## 4. 下一輪自家研究提案(聚焦 3 個,皆為交互不新開維度)

> 定位:這**不違反** STATE_OF_DASHBOARD 的「STOP 廣度搜索」——這三個都是**把已有維度做交互/過濾**,不是開第 17 個新維度。且都掛在既有可部署系統 C 上,失敗即確認、成功即升級 champion。三個以外一律不做。

### 提案 1(最高優先)★ champion 加「現貨 hedge-short 過濾腳」
- **假設**: champion(期貨 z60>0)在「現貨同向(cashForeign≥0)」時比「現貨背離(現賣但期多=避險)」時 next-day 顯著更強;過濾掉 hedge-short 誤讀可降尾。
- **做法**: 在 panel 上把 champion 多頭日切成「期現同向 / 期現背離」兩桶,比 forward 報酬 + Sharpe + maxDD;permutation 檢定「同向桶 > 背離桶」;算 ΔDSR vs 裸 champion。**嚴驗**: 用既有 16-trial 家族 penalty 一致計 DSR,OOS 時間切分同系統C,要求正交性(與 champion 相關 + 增益在量測誤差外)。
- **為何值得**: champion 唯一單腳、成本零、三路專家共識、最可能真升級。**預期**: 小幅正增益或 null;若 null 就把 champion 單腳定案。

### 提案 2 ★ 恐慌 stack 的「多維同時對齊」contrarian 觸發(非溫度計)
- **假設**: 「P/C 高 z + VIX z>2 + breadth ma50 極低」**三者同時**觸發後 N 日(5/10/20)大盤 forward 為正(contrarian 底),優於任一單維。
- **做法**: 定義三維同時極端的稀有觸發集(預期 n 極小 <15),bootstrap/permutation 檢定 forward>0;明確標註 rarity 與樣本外不可外推。**嚴驗**: 因 n 小,用 block-bootstrap + 誠實宣告「regime 觸發器非 alpha」;對照 crash-thermometer 的 fresh-event 失敗,證明「同時對齊」是不同(更嚴)的假設。
- **為何值得**: 補「我們只有 VIX 單閘」的唯一缺口;與已部署 VIX gate 同源、可疊加。**預期**: 觸發極稀有,可能只確認 VIX 單閘已夠、多維無增量。

### 提案 3 ★ Asquith 2005 台股複製:券資比 × 大戶持股供給約束雙排序
- **假設**: 只有「高券資比(放空需求高)× 低千張大戶持股(借券供給緊)」的緊約束子集,forward 報酬顯著為負(空頭選股腳);其餘 95% 股票交互無效。
- **做法**: 全宇宙 double-sort(券資比 tercile × 大戶持股 tercile),看最緊約束角落 vs 其餘的等權多空 forward 報酬;permutation + DSR;明確報告命中面寬度(預期很窄)。**嚴驗**: 台股 mapping 不完美(融資≠short interest),先做 construct-validity 檢查(券資比是否真代表借券約束);winsorize;OOS 時間切分。
- **為何值得**: 唯一「頂級期刊背書 + 台股從沒人做 + 復活兩個已證偽單維」的組合。**預期**: 可能找到一個窄但真的空頭選股腳,或確認台股融資結構讓此交互不成立。

**明確不做**: 分點吸貨結構、頂背離、軋空、投信認養、阿斯匹靈框架複製——皆已測過或屬動能偽裝/坊間敘事,期望報酬低於執行成本。

---

## 5. 回傳摘要

**專家最推的前 5 組合(按實證強度×我方相關性):**
1. positioning × 波動regime(Daniel-Moskowitz 2016,強)— ✅ 已做 VIX gate DSR 0.998
2. positioning × 趨勢regime gating(強)— ✅ 已做 MA200 DSR 0.995
3. short interest × 機構持股供給約束(Asquith 2005 JFE,強)— ❌ **沒做,缺口C**
4. P/C × VIX × breadth 恐慌 stack(中)— ⚠️ **只有VIX單閘,缺口B**
5. 外資現貨 × 期貨 hedge-short 過濾(中)— ⚠️ **champion單腳,缺口A**

**我們還沒測的缺口(僅 3 個通過篩選):**
- A. champion 加現貨 hedge-short 過濾腳(成本零、最可能真升級)
- B. 恐慌 P/C×VIX×breadth 多維「同時對齊」contrarian(補 VIX 單閘唯一缺口)
- C. Asquith 2005 券資比×大戶持股雙排序(頂刊背書、台股從沒人做)
- 其餘專家組合全已測過或屬坊間/動能偽裝(分點吸貨=買上升趨勢、頂背離=事後敘事、軋空/投信認養=無驗證)

**下一輪提案摘要:** 三個交互實驗(非新開維度、皆掛系統C):①champion×現貨hedge-short過濾 ②恐慌stack多維同時對齊觸發 ③Asquith券資比×大戶供給約束雙排序。全部用既有 16-trial DSR penalty + OOS 時間切分嚴驗,失敗即確認 champion 單腳/VIX 單閘已足、成功即升級。與 STATE_OF_DASHBOARD「STOP 廣度搜索」相容——這是深化交互不是加維度。誠實預期:三者多半確認現狀,提案 1 最可能有小幅正增益。

_檔案: `/Users/jackm4/Documents/ETF/股票研究/reports/research/dashboard-completeness/expert_combination_shortlist.md`_
