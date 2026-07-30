# 全方位觀盤儀表板 — 完整性研究 Master 索引與整合設計

> 建立日期：2026-07-30 ｜ 首席量化研究員彙整
> 本目錄把「已深耕的 5 大命名維度」與「這次補齊的 11 個維度研究」組裝成一份 master 索引 + 分層整合設計。
> 每一份子報告都走同一套證偽式驗證框架：**IS/OOS 切分 + permutation(同曝險隨機對照) + Deflated-Sharpe(DSR) + regime-conditioning + 對 champion 共線檢定**。
> 全專案唯一通過所有關卡的訊號是 **champion = 外資台指期淨未平倉 positioning（領先）**；其餘維度大多是「動能偽裝」或「裝飾/確認/前兆」用途,不是獨立 alpha。

---

## 1. 摘要 — 這次補了哪些維度

這次的目標是把「觀盤要看的全部東西」逐一盤點、逐一做正式驗證,弄清楚每個維度到底是**真訊號**還是**動能偽裝**,以及**能不能實作**(資料是否到位)。

### 1a. 這次新做的 11 個維度（dashboard-completeness 子報告）

| 維度 | status | 領先/同步/落後 | 一句話結論 | 報告 |
|---|---|---|---|---|
| 技術趨勢/均線/Weinstein 階段（L0 regime gate） | ✅ 已實跑 | 同步→落後 | 本質是價格動能,單獨無 alpha(perm p=0.13);**唯一真貢獻是當 L0 閘門過濾 champion**(close>MA200 把 OOS Sharpe +1.81→+2.61、maxDD −18.5%→−3.9%、DSR→0.995) | [tech_regime.md](./tech_regime.md) |
| 相對強度 RS（個股 vs 大盤） | ✅ 已實跑 | 同步偏落後 | 四種 RS 中僅 Weinstein Mansfield RSM 非動能偽裝(perm p=0.002、與 champion 正交 −0.11),但 IC 近零、空頭 Sharpe −0.69 → L1 個股強弱確認濾網,非 standalone alpha | [relative_strength.md](./relative_strength.md) |
| 量價結構（成交值/量能/帶量突破） | ✅ 已實跑 | 同步(confirmation) | 大盤層單獨無 alpha(最佳 pvt_slope DSR 0.474 fail、殘差化後 IC 塌到 0.01);只當「趨勢可信度」儀表註記與量價背離前兆,不加碼 | [price_volume.md](./price_volume.md) |
| 市場廣度/騰落（ADL/%>MA/McClellan/Zweig） | ✅ 已實跑 | 同步(regime 燈) | 最佳 pct_ma200_z60 OOS +1.24 但殘差 IC≈0.013、DSR 0.448 fail、edge 全來自多頭曝險 → 是 Stage-4 margin 的翻版;僅接 L0 一致性確認燈 | [breadth.md](./breadth.md) |
| 期貨進階（大額交易人 OI + 台指期 basis） | ✅ 已實跑(真資料) | 領先但與 champion 同格 | **[P2 真跑 n=1986]** 大額 net = champion 的冗餘鏡像(corr 0.60–0.65)、散戶殘差數學上 = −(前十大 net)、basis 線性/z60 OOS 皆負且 IC≈0 perm p 0.80–0.89;連 champion 自身 DSR 都 0.593 fail → 不採信為 alpha,只當同義佐證顯示欄 | [futures_positioning.md](./futures_positioning.md) |
| 選擇權籌碼（P/C、Max Pain、外資買賣權、小台散戶多空比） | ✅ 已實跑(真資料·部分) | 領先(共線)+微結構 | **[P2 全史 TXO/MTX 真資料]** 最強 S3 PCR_OI OOS +1.22/perm p=0.004 但被 DSR 0.845+殘差IC≈0 雙殺為動能偽裝;唯一保留微弱正殘差(+0.023)的 S1 外資選擇權淨是 champion 的選擇權回聲;四訊號 DSR 全 <0.95 無一過檢定。**仍缺**:大額交易人買賣權 + 乾淨到期序列版 | [options_micro.md](./options_micro.md) |
| 融資維持率（整戶擔保維持率/斷頭溫度計） | ✅ 已實跑(真資料) | 落後/同步+尾部前兆 | **[P2 官方整戶擔保維持率 ground-truth n=1985]** 系統化 MR_z60 是動能偽裝(corr 0.79、殘差IC由+0.027翻−0.012、perm p=0.215+DSR 0.258 皆未過);唯深尾帶真「反向」marker(MR_z60≤p05 fwd20 +1.16%、絕對<150% +2.26%,事件級定性警示非可加碼 alpha)。注:原 proxy 尾部 lift 與真資料相反,證明必須用真資料。**仍缺**:個股層近斷頭廣度前兆 | [margin_maintenance.md](./margin_maintenance.md) |
| 信用微結構（借券賣出/當沖比重/融資使用率） | ⛔ 待接資料 | SBL 領先/當沖同步/使用率落後 | 三 headline 本地皆不可算(需 FinMind);錨定用市場券資比 proxy 實跑 OOS 僅 +0.27、perm p=0.48 → 全家族=弱濾網非 alpha | [short_daytrade.md](./short_daytrade.md) |
| 大戶持股集中度（千張大戶/集保戶數/董監質押） | ✅ 已實跑(真資料·部分) | 同步偏前兆 | **[P2 真集保 HoldingSharesPer n=28,880 stock-week、164 檔]** 三子訊號(千張大戶Δ/z/合成集中)全 null:IC≈0、去動能 partial IC 歸零翻負(corr mom 0.21–0.23)、perm p 0.26–0.77、DSR 全 fail;與 champion 正交(corr −0.075)。乾淨證偽。**仍缺**:中小型股(真主力最可能翻案段)+ 2018–22 回補 + 董監質押 | [holder_concentration.md](./holder_concentration.md) |
| ETF 申贖/受益人數/折溢價 | ✅ 已實跑(真資料·部分) | 領先但 CONTRARIAN | **[P2 真集保 8 檔 ETF n=2548]** 大盤層散戶擁擠 6 變體(受益人數增率/SHOUT申贖/人均持有)全無穩健 edge:IS IC≈0、方向先驗符號不一致、唯一 OOS 正的 hg_all perm p=0.077 未過;動能偽裝已排除=乾淨雜訊。combo 混用拖累 champion。**仍缺**:S2 折溢價(FinMind 無官方 NAV,422 拒絕)+ 個股/類股橫斷面 | [etf_flows.md](./etf_flows.md) |
| 國際總經錨（VIX/DXY/US10Y/USDTWD/Fed） | ✅ 已實跑(真資料·部分) | L0 regime + 事件 gate | **[P2 Yahoo 四錨真序列 n=1986]** 連續因子全無訊號(|IC_OOS|≤0.08、perm p 0.08–0.46),表面高 Sharpe/DSR 是 long-flat 吃漂移+重疊視窗自相關假象;唯一真效果=VIX 事件式恐慌 gate(60日z>+2.0 → 次日 TAIEX −0.099% vs 平時+0.088%,perm p=0.035,與 champion 非冗餘 corr±0.03)。DXY/US10Y/USDTWD 無訊號僅當風險背景燈。**仍缺**:Fed(FRED)+ 在地 VIXTWN | [global_macro.md](./global_macro.md) |

**status 統計（Phase-2 真資料補實後,2026-07-30）：10 已實跑(真資料) / 1 待接資料。** 其中 6 個維度本輪由 proxy/待接 升級為真資料實跑(期貨進階·選擇權·融資維持率·大戶集中度·ETF·國際總經),4 個標「部分」= 主市場層已真跑但仍缺子維度(見各列「仍缺」)。唯一仍全待接 = 信用微結構(借券/當沖/使用率,需 FinMind resync)。**真資料補實後淨新增獨立 alpha = 0**:六維度全數落入「動能偽裝 / champion 冗餘鏡像 / 乾淨 null」,champion 仍是唯一領先 alpha。

### 1b. 已深耕的 5 大命名維度（既有研究,分散在 chip-macro / branch 目錄）

| 維度 | status | 領先/同步/落後 | 核心結論 |
|---|---|---|---|
| 分點 branch footprint | ✅ have | 前兆(廣度)/個股 overlay | 核心產品 = fade-veto 名單(30 branch)+ 3 席穩健跟單(2344/8046/8358);方法論教訓:多數個股 pool 是 overfit,資料一擴充就露餡(980T/adopted-44 pattern) |
| 籌碼綜合（chip composite L2 core） | ✅ have | 領先/同步/落後皆有 | chip alone OOS Sharpe +1.64、chip×bull +2.49;分層系統(L0-L3)方法論骨幹沉澱於此 |
| 三大法人（現貨淨買超） | 🟡 partial | 同步 | 外資現貨=同步指標,單獨資訊量弱(IC 弱於期貨 OI);缺專門的獨立維度證偽報告(多當配角) |
| 期貨未平倉 OI（champion） | ✅ have | **領先** | 全專案唯一 champion;fut_foreign_oi_z60>0 OOS +1.12;誠實下修:DSR borderline 0.869、空頭年失效 = 弱 risk-on regime filter 非 standalone alpha |
| 融資餘額 margin | ✅ have | **落後** | 對大盤同步偏落後(峰值落後 11 天),去趨勢後相關僅 0.006 = 無獨立解釋力;真正訊號在券資比(軋空) |

> 相關既有報告路徑（絕對）：
> - `/Users/jackm4/Documents/ETF/股票研究/reports/research/chip-macro/RESEARCH_LOG.md`（Stage 1-8）
> - `/Users/jackm4/Documents/ETF/股票研究/reports/research/chip-macro/LAYERED_DESIGN.md`（L0-L3 設計）
> - `/Users/jackm4/Documents/ETF/股票研究/reports/research/chip-macro/融資餘額_2026H1_研究.md`
> - `/Users/jackm4/Documents/ETF/股票研究/reports/research/branch-footprint-screen/`（分點全套）
> - `/Users/jackm4/Documents/ETF/股票研究/config/branch_fade_veto.json`（fade-veto 名單）

---

## 2. ★ 全方位分層整合設計（16 維度歸位 L0→L1→L2→L3）

核心理念：**沒有單一訊號能同時做「擇時 + 選股 + 風控」。** 把 16 個維度依「在決策鏈中的角色」放進四層,每層問一個不同的問題。唯一的日頻大盤領先 alpha 是 champion(外資期貨 positioning);其餘維度的價值是**過濾 / 確認 / 前兆 / 選股**,不是並列的第二支擇時腳。

### 四層職責

| 層 | 問題 | 職責 |
|---|---|---|
| **L0 Regime** | 「現在能不能站多方?」 | 大環境風險閘門。價≤MA200 / Stage4 / VIX spike / 廣度<50% 時 veto 重新武裝多單 |
| **L1 價量** | 「趨勢可不可信?」 | 個股橫斷面強弱 + 量價確認。給 champion 的方向做「橫斷面支撐」與可信度背書 |
| **L2 籌碼核心** | 「聰明錢往哪站?」 | 領先 positioning(champion)+ 各籌碼配角 + 反向鏡像(ETF 笨錢) |
| **L3 微結構** | 「短線情緒過熱/事件了嗎?」 | 結算週、散戶擁擠、當沖過熱、斷頭 flush 等事件級前兆 |

### 各層成員與角色

**L0 — Regime Gate（決定能否放行多單）**
| 成員 | 角色 | 備註 |
|---|---|---|
| 均線 / Weinstein 階段（close>MA200 / MA150+slope） | 同步→落後 · **過濾** | 唯一真貢獻:gated champion 是全 study 唯一 DSR>0.95 者 |
| 市場廣度 pct_above_ma200>50% | 同步 · **regime 燈** | 兩者同綠才高信賴;champion 綠但廣度<50%(少數權值撐盤)= 降級提示 |
| VIX（待接） | 前兆(spike)+同步 · **恐慌 gate** | 下一步性價比最高的補件 |
| DXY / US10Y / Fed 路徑（待接） | 低頻領先 · **風險 gate** | 天然銜接 champion 的 chip×bull 條件化 |
| ETF 受益人數（待接） | 落後→極端轉前兆 · **反向情緒濾網** | 受益人數創高+人均持有下降 → froth 減碼 |

**L1 — 價量層（決定站多方時抱哪些、可不可信）**
| 成員 | 角色 | 備註 |
|---|---|---|
| 相對強度 RS（Weinstein RSM） | 同步偏落後 · **個股強弱確認** | 與 champion 正交 −0.11;champion 決定市場、RS 決定個股 |
| 量價結構（帶量突破/vol_z） | 同步 · **可信度確認** | 只展示不加碼;量價背離入前兆格 |
| 廣度 net_breadth / ADR / McClellan | 同步 · **一致性確認** | regime 一致性檢查,不新增部位 |
| SOX/SMH/ADR 開盤 gap | 機械式領先→同步 · **開盤前兆** | 弱開盤別追多;intraday 續勢已證是動能偽裝 |
| 新台幣匯率 TWD（待接） | 同步 · 恐與外資冗餘 | 納入前須證相對 foreign 的淨增量 |

**L2 — 籌碼核心（聰明錢 positioning）**
| 成員 | 角色 | 備註 |
|---|---|---|
| **外資台指期淨未平倉 OI（champion）** | **領先** · **定方向的唯一 alpha** | fut_foreign_oi_z60>0 做多;chip×bull OOS +2.49 |
| 籌碼綜合 chip composite | 領先/同步 · **主引擎** | chip alone +1.64 |
| 三大法人現貨淨買超 | 同步 · **佐證** | 外資現貨=同步,單獨弱 |
| 融資餘額 margin | 落後 · **froth flag** | 動能代理;不當獨立腳 |
| 融資維持率 / 近斷頭廣度 | 落後+尾部前兆 · **斷頭溫度計** | champion 說多但近斷頭廣度飆升 → 提示回檔恐轉多殺多 |
| 大額交易人 OI / basis | 領先(共線) · **同義顯示欄** | champion 的鏡像,不進 panel |
| 外資選擇權淨部位 | 領先(共線) · **確認/加權** | combo 略勝但共線,不當獨立腳 |
| 借券賣出 SBL（待接） | 領先 · **個股知情做空過濾** | 唯一有學術背書;補 champion 缺的現貨個股空方維度 |
| 大戶持股集中度（待接） | 同步偏前兆 · **選股** | champion 綠燈日 × 集中度 top-decile 選股 |
| 董監質押（待接） | 揭露落後+下檔尾部前兆 · **risk-off veto** | 高質押 tail 燈疊在任何多方訊號之上否決 |
| ETF 申贖 flow（待接） | 領先但 **CONTRARIAN** · **對立腳** | 與 champion 放同一 regime 引擎的對立兩端 |

**L3 — 微結構（短線情緒/事件前兆）**
| 成員 | 角色 | 備註 |
|---|---|---|
| 分點賣超/買超廣度 | 前兆 · **事件格** | 大跌溫度計 H4 / 大漲買超廣度;需事件化人工判讀 |
| 選擇權 PCR / Max Pain | 同步偏落後情緒 / 非方向 | 結算週啟用;極端分位當多單反向減碼閘門 |
| 小台散戶多空比 | 落後反指標 · **散戶擁擠前兆** | OOS 僅 +0.37 不顯著,觀察型 |
| 當沖比重 | 同步 · **過熱濾網** | 入場/風險濾網 |
| ETF 折溢價 | 領先/前兆 · **超短 mean-revert** | 當申贖 flow 的閘門(溢價日申購才算數) |
| Zweig Breadth Thrust | 領先味 · 觀察型 | 本地 8 年僅 2 事件,無統計力 |

### 「怎麼搭配看」— 決策流

```
每日開盤前 / 盤後,依序問四個問題:

┌─ ① L0 REGIME:現在能不能站多方? ──────────────────────────┐
│   • 大盤 close > MA200 且未翻 Stage4?                        │
│   • VIX 未 spike?廣度 %>MA200 > 50%?                        │
│   • DXY/US10Y/Fed 未轉急殺 risk-off?                        │
│   → 全過 = 放行;任一 veto = 只做防禦、不重新武裝多單         │
└──────────────────────────────────────────────────────────┘
                          │ 放行
                          ▼
┌─ ② L2 領先定方向:聰明錢往哪站? ─────────────────────────┐
│   • CHAMPION 外資期貨 OI z60 > 0 → 偏多方向                  │
│   • chip composite / chip×bull 同向 → 信賴加強               │
│   • ETF 申贖 flow(反向鏡像)若同時「散戶狂買」→ 對立扣分   │
└──────────────────────────────────────────────────────────┘
                          │ 方向 = 多
                          ▼
┌─ ③ L1 同步確認:趨勢可不可信、抱哪些? ──────────────────┐
│   • 廣度 > 50% 給橫斷面支撐(< 50% = 少數權值撐盤,降級)    │
│   • 個股 RS(Weinstein RSM)排名 + 帶量突破 → 選抱誰        │
│   • L2 選股:champion 綠 × 大戶集中度 top-decile             │
└──────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─ ④ 落後當 FROTH FLAG + 前兆抓事件 ───────────────────────┐
│   落後(減碼提示,不反手):                                  │
│     • 融資餘額飆 / 融資維持率近斷頭廣度飆 → froth,回檔風險  │
│     • 受益人數創高+人均下降 → 笨錢擁擠                       │
│   前兆(事件級,人工判讀):                                  │
│     • 分點賣超廣度飆 / 量價背離 / 散戶多空比極端 / 高質押    │
│     • 結算週 PCR·MaxPain / 借券賣出激增個股 → 迴避           │
└──────────────────────────────────────────────────────────┘
```

一句話：**L0 決定「能不能」、L2-champion 決定「往哪」、L1 決定「抱誰 + 可不可信」、落後與前兆決定「何時該警戒與減碼」。** 只有 champion 是能獨立定方向的領先 alpha,其餘全是圍繞它的過濾、確認、選股、警示。

---

## 3. 資料缺口與優先補齊順序（backlog,依投報率排序）

排序原則：**訊號價值 ÷ 接資料成本**。乾淨免費 API + 能解鎖 L0 風控或補 champion 缺的維度 = 優先;需自建爬蟲/組裝且訊號已證偽 = 暫緩。

> **Phase-2(2026-07-30)更新:backlog #1、#3–#8 已用真資料實跑解決(全數證偽,無新增 alpha);#11/#14 部分解(受益人數/散戶多空比已跑,折溢價 NAV/大額買賣權仍缺)。狀態欄 ✅=已解、🟡=部分、⛔=仍缺。**

| # | 狀態 | 待補資料源 | 解鎖維度 | 成本 | 價值 | 備註 |
|---|---|---|---|---|---|---|
| 1 | ✅已解 | **Yahoo `^VIX`**(或在地 VIXTWN) → daily_bars | L0 恐慌 gate | 極低 | 高 | **P2 已抓真序列**;VIX 60日z>+2.0 事件 gate perm p=0.035 真效果(與 champion 非冗餘)。仍缺在地 VIXTWN(去美盤時差) |
| 2 | ⛔仍缺 | **FinMind `TaiwanFuturesInstitutionalInvestors` resync** | champion 本地 stale 至 2026-07-08 | 極低 | 高 | champion 資料必須是新的才有用,優先 resync(P2 用既有 panel 未 resync) |
| 3 | ✅已解 | **PCR_OI/PCR_vol**(P2 用 FinMind `TaiwanOptionDaily` 逐年 range) | 選擇權 PCR(L3 情緒) | 低 | 中 | **P2 全史真跑**;PCR_OI OOS +1.22 但 DSR 0.845+殘差IC≈0 雙殺=動能偽裝 |
| 4 | ✅已解 | **FinMind `TaiwanStockHoldingSharesPer`**(保留 people) | 大戶集中度 + ETF 受益人數 | 低 | 中 | **P2 一次解鎖兩維度真跑**;大戶集中度 164 檔 null、ETF 受益人數乾淨雜訊 |
| 5 | 🟡部分 | **USD/TWD**(P2 用 Yahoo `USDTWD=X`,非台銀牌告) | 國際錨 TWD | 低 | 中 | **P2 已抓**;與 foreign corr 僅 −0.18 非冗餘但本身無 edge。仍缺台銀牌告官方源 |
| 6 | ✅已解 | **Yahoo `^TNX`(US10Y) + `DX-Y.NYB`(DXY)** → macro_daily | US10Y / DXY(L0 低頻) | 低 | 中 | **P2 已抓真序列**;連續因子 IC≈0 perm 不顯著,僅風險背景燈。仍缺 Fed(FRED FEDFUNDS+FOMC 行事曆) |
| 7 | ✅已解 | **FinMind `TaiwanFuturesDaily`(TXF 近月)** | 台指期 basis | 中 | 低 | **P2 已抓真跑**;basis 線性/z60 OOS 皆負 IC≈0 perm p 0.80–0.89,僅儀表數字。用 position 段 close 非結算價 |
| 8 | ✅已解 | **FinMind `TaiwanFuturesOpenInterestLargeTraders`** | 大額交易人 OI | 低 | 低 | **P2 已抓真跑**;已證 = champion 冗餘鏡像(corr 0.60–0.65),僅顯示欄 |
| 9 | ⛔仍缺 | **FinMind `TaiwanStockSecuritiesLending` + TWSE TWT93U** | 借券賣出 SBL(L2 個股空方) | 中 | 中 | 唯一有學術背書的個股知情做空維度,**Phase-3 最高優先** |
| 10 | ⛔仍缺 | **FinMind `TaiwanStockDayTrading`** | 當沖比重(L3) | 低 | 低 | 本地 stock_daytrade_daily volume 全 NULL,需重抓 |
| 11 | 🟡部分 | **ETF NAV/折溢價**(受益人數已解,FinMind 無官方 NAV,422 拒絕) | ETF 申贖/折溢價 | 中-高 | 中 | 受益人數/申贖(SHOUT)P2 已跑(乾淨雜訊);S2 折溢價仍缺官方 NAV 源(TPEX/SITCA) |
| 12 | ⛔仍缺 | **1010-branch universe backfill**(data/replica) | 分點完整宇宙(2024-07 前) | 高 | 中 | 完整 universe 僅 2024-07+,更早需回補 |
| 13 | ⛔仍缺 | **MOPS 董監質押爬蟲**(t13sa01,月頻) | 董監質押(risk-off veto) | 高(反爬+POST) | 中 | 下檔尾部前兆,月頻;大戶集中度維度標為待補子訊號 |
| 14 | 🟡部分 | **TAIFEX 大額交易人買賣權 / MTX 散戶殘差** | 選擇權大額 + 小台散戶 | 中 | 低 | **P2 已跑 MTX 散戶多空比(perm p=0.237 不顯著)**;大額買賣權(前5/10大,最純大戶領先候選)仍缺 |
| 15 | ⛔暫緩 | **融資限額表(股本×融資成數)** | 融資使用率 | 極高 | 極低 | **建議暫緩**,資料源最弱、訊號已證弱 |
| 16 | ⛔仍缺 | **個股層近斷頭部位廣度**(FinMind `TaiwanStockMarginPurchaseShortSale` 全量) | 融資維持率個股前兆 | 中 | 中 | 市場單線答不出廣度分佈;本地融資 universe 僅 ~176/1000 檔且多停 07-07,scaffold 待回填 |

> **Phase-2 後的真缺口(Phase-3 batch）:** ⛔ 借券賣出 SBL(#9,唯一學術背書個股空方,最高優先)、champion resync(#2,保鮮)、個股近斷頭廣度(#16)、當沖(#10)、分點完整宇宙回補(#12)、董監質押(#13)、選擇權大額買賣權(#14 剩半)、ETF 折溢價 NAV(#11 剩半)、Fed/VIXTWN(#1/#6 剩半)。已解的 #1/#3–#8 全數證偽,證明「補資料 ≠ 找到 alpha」——真資料的價值是**乾淨證偽**,擋掉了 6 個原本可能被誤採的動能偽裝維度。

---

## 4. 用今天（7/30）實況示範:這套儀表怎麼「解釋新聞與現象」

今日盤面出現數個看似矛盾的訊號同時發生。用四層框架拆解,矛盾就消失了——**它們分屬不同層、扮演不同角色,不該放在同一個天平上比大小。**

**現象合流：外資期貨深空 82k(領先)+ 融資投降(落後)+ 年線之上未翻空(regime)+ 拉積盤/公股護盤 + Fed 鷹派/循環交易疑慮(國際錨)。**

| 觀察到的現象 | 屬哪一層 | 角色 | 儀表怎麼讀 |
|---|---|---|---|
| **外資台指期淨空單 −82k(深空)** | L2 champion | **領先** | fut_foreign_oi_z60 深負 = champion 明確**偏空/風險方向**。這是唯一能定方向的 alpha,今天它壓盤方向清楚,不宜重新武裝多單 |
| **融資餘額投降式下殺、近斷頭洗淨** | L2 落後 | **froth flush 前兆** | 融資是**落後**指標(峰值落後 11 天),它「投降」代表的是**過去的槓桿在被清洗**,不是未來方向。近斷頭廣度飆 = 斷頭溫度計亮,提示「多殺多 vs 續殺」的尾部風險,但不能拿它當抄底訊號(日級 z-score contrarian 已證失敗) |
| **加權指數仍在年線(MA200)之上、未翻 Stage4** | L0 regime | **gate 尚未 veto** | L0 尚未轉空 = 系統還沒到「全面退場」;但這是**同步/落後**訊號,不能因為「還在年線上」就無視 champion 的領先深空。L0 的職責是「不放行新多單」不是「保證安全」 |
| **拉積盤 / 公股護盤(權值撐盤)** | L1 廣度 | **一致性警訊** | 若指數靠少數權值撐、而廣度 %>MA200 < 50% → 這是典型「champion 綠但廣度背離」的**降級提示**:指數的強是假象,橫斷面沒有支撐 |
| **Fed 鷹派 / 循環交易(cyclical rotation)疑慮** | L0 國際錨 | **低頻 risk gate** | DXY 走強、US10Y 上行、Fed 鷹派 → risk-off regime 條件化,天然壓抑 champion 的 chip×bull 觸發。這解釋了為何外資在期貨端提前站空 |

**整合判讀(這就是儀表的價值)：**
> 今天不是「多空矛盾」,而是一個**內部一致的 risk-off 畫面**——
> ① 國際錨(L0)先轉 risk-off(Fed 鷹派/循環交易疑慮)→
> ② 外資 champion(L2 領先)在期貨端提前站到 −82k 深空,壓盤方向明確 →
> ③ 指數靠公股/權值硬撐(L1 廣度背離)= 表面強、橫斷面弱 →
> ④ 融資投降 + 近斷頭洗淨(L2 落後 froth flush)是**過去槓桿被清算的落後確認**,不是見底訊號 →
> ⑤ 年線之上未翻空(L0)只代表「還沒到全面退場」,不代表安全。
>
> **結論:領先(champion 深空)+ 前兆(廣度背離)一致偏空,落後(融資投降)在確認下殺已發生,regime(年線)尚未給 veto 但也不給放行。** 儀表的正確動作 = **不追多、不抄底、等 champion 期貨回補或指數重新站上 + 廣度回到 50% 以上再談重新武裝。** 融資投降容易誘人「洗完就反彈」,但它是落後訊號,不能單獨作為進場依據。

---

## 5. 誠實結論

### 5a. 哪些維度真能加值
- **外資台指期 positioning(champion)** — 全專案唯一經完整 IS/OOS+permutation 驗證、能獨立定方向的領先 alpha。是整個儀表的錨。
- **L0 regime gate(close>MA200 / Stage)** — 本身無 alpha,但**當過濾器對 champion 的貢獻是真實且唯一通過 DSR>0.95 的**:把 gated champion 的 maxDD 從 −18.5% 壓到 −3.9%。這是「1+1>2」的少數真案例。
- **廣度一致性檢查(%>MA200)** — 當 regime 一致性燈,抓「少數權值撐盤」的假強,有真實的降級價值。
- **借券賣出 SBL(待接)** — 唯一有學術背書、能補 champion 缺的「個股知情做空」維度,值得投資接資料。
- **分點賣超廣度 / 融資近斷頭廣度** — 事件級**前兆**格有真實價值,但需人工判讀、非連續 alpha。

### 5b. 哪些多半是動能偽裝 / 裝飾
- **相對強度 RS、量價結構、市場廣度(擇時用)、融資餘額、融資維持率** — 反覆出現同一模式:**表面 OOS Sharpe 不錯,但殘差化(對價格動能正交)後 IC 塌陷、DSR 不過、edge 全來自多頭曝險。** 它們是「被 normalize 過的價格動能」,不是獨立訊號。當**確認/選股/froth flag** 有用,當**獨立擇時腳**是自欺。
- **大額交易人 OI、台指期 basis、外資選擇權淨部位、小台散戶多空比** — 全是 champion 的**共線鏡像**(corr>0.4)或數學恆等式(散戶殘差 = −前十大 net)。加進 panel 只會重複計算同一資訊,製造假分散。只配當「同義佐證顯示欄」。
- **SOX/ADR intraday 續勢** — 開盤 gap 是機械式已 priced,intraday edge 對 TAIEX 動能+外資流正交化後完全崩解(DSR 0.005)。純裝飾。
- **大戶集中度 proxy、ETF flow** — 目前資料不足;proxy 已證偽。ETF flow 有理論價值(反向鏡像)但需先接乾淨資料才能定論。

### 5c. 貫穿全研究的方法論教訓
1. **殘差化是照妖鏡** — 任何訊號都要問「對價格動能正交化後還剩什麼」。剩下近零 = 動能偽裝。
2. **regime-conditioning 是鐵律** — 幾乎所有籌碼/技術訊號都只在多頭有效、空頭失效。不做 regime 條件化的 Sharpe 是假的。
3. **共線 > 分散的假象** — 多個「不同」訊號其實測同一件事(外資 positioning),加總不增加資訊只增加過擬合。
4. **不要網格搜尋** — 呼應 980T / adopted-44 過擬合史:資料一擴充就露餡。DSR 就是為了懲罰這種 search-inflation。
5. **前兆 ≠ alpha** — 事件級前兆(分點廣度、斷頭 flush、質押 tail)有觀盤價值,但是離散事件、需人工判讀,不能塞進連續回測當擇時腳。

### 5d. 下一步（依 backlog 排序）
1. 接 **VIX**(#1)補 L0 最後一個恐慌 gate + **resync champion**(#2)保鮮。
2. 接 **TAIFEX PutCallRatio**(#3)與 **FinMind HoldingSharesPer**(#4,一次解鎖大戶集中度+ETF 受益人數)。
3. 把已實跑的 gated-champion(L0×champion)正式落地到 regime 日報,作為系統唯一 DSR>0.95 的組合。
4. **跨空頭週期再驗** — gated champion 的漂亮數字來自單一多頭窗(OOS 596 日僅 70 空頭日),下一個空頭週期是真正的 OOS 考場。
5. 個股層 Stage2/RS 正式驗證前,**先補 FinMind TaiwanStockPriceAdj + 下市宇宙**除存活者偏誤(全專案已知盲點)。

---

## Phase-2 真資料補實（2026-07-30）

Phase-1 有 4 個維度是用 proxy 或本地不完整資料跑的、3 個是純待接。Phase-2 針對其中 **6 個維度改用真資料(FinMind REST / Yahoo Finance 官方序列)重跑正式驗證**,把「proxy 說的話」換成 ground-truth。核心發現:**真資料補實後淨新增獨立 alpha = 0**——六維度全數落入動能偽裝、champion 冗餘鏡像、或乾淨 null。真資料的價值不是找到新 alpha,而是**乾淨證偽**,擋掉 6 個原本可能被誤採的維度;其中融資維持率一案更直接推翻了 proxy 的結論(proxy 尾部 lift 為負,真資料為 +1.16%,方向相反)。

驗證框架同 Phase-1:panel 2018-06→2026-07-29(n≈1986)、70/30 或固定日期 IS/OOS(OOS≈596 日)、B&H OOS Sharpe +0.39、champion OOS +1.12;每個訊號過 permutation(同曝險)+ 殘差IC(扣 champion+20日動能)+ Deflated-Sharpe(需 >0.95)+ 對 champion 共線檢定。

| 維度 | 資料源(真) | rows | 最強訊號 / 頭條數字 | 決定性證偽 | verdict / 落層 |
|---|---|---|---|---|---|
| **融資維持率** 整戶擔保維持率 | FinMind `TaiwanTotalExchangeMarginMaintenance`(官方單一線,ground-truth) | 2,082(2018-01→2026-07 無缺口) | MR_z60 corr(價格動能)=+0.791;OOS long/flat +0.53 | 殘差IC +0.027→−0.012(縮57%變號);perm p=0.215、DSR 0.258 皆未過;與 champion corr 0.079 正交無增量 | 動能偽裝(系統化層)+ 深尾真「反向」marker(MR_z60≤p05 fwd20 +1.16%、絕對<150% +2.26%,事件級定性)。落 **L2** 受 L0 閘控。**推翻 proxy**(proxy 尾部反向) |
| **選擇權籌碼** TXO/MTX 全套 | FinMind `TaiwanOptionInstitutionalInvestors`/`TaiwanFuturesInstitutionalInvestors`+`Daily`/`TaiwanOptionDaily` | 2,085(PCR/MaxPain 全史) | S3 PCR_OI OOS +1.22 / perm p=0.004(頭條最強) | S3 DSR 0.845 + 殘差IC −0.008 雙殺=動能偽裝;S1 外資選擇權淨唯一保留正殘差 +0.023 但 = champion 選擇權回聲;四訊號 DSR 全<0.95 | 動能偽裝為主,無獨立 alpha 過 DSR。S1 屬 champion 確認/加權型。落 **L2/L3**。教訓:perm 顯著≠真 alpha(不控動能共線),DSR+殘差IC 才決定性 |
| **國際總經錨** VIX/DXY/US10Y/USDTWD | Yahoo Finance v8 chart API 直打(^VIX/DX-Y.NYB/^TNX/USDTWD=X) | ~8,700(四錨各~2150) | 連續因子表面 OOS Sharpe 1.2–3.2 | 連續因子 |IC_OOS|≤0.08、perm p 0.08–0.46 全不顯著(高 Sharpe=long-flat 吃漂移+重疊視窗自相關假象) | 唯一真效果=**VIX 事件式恐慌 gate**(60日z>+2.0 → 次日 TAIEX −0.099% vs +0.088%,perm p=0.035,與 champion corr±0.03 非冗餘)。落 **L0** veto;DXY/US10Y/USDTWD 無訊號當背景燈 |
| **期貨進階** 大額交易人 OI + basis | FinMind `TaiwanFuturesOpenInterestLargeTraders`+`TaiwanFuturesDaily`(TX) | 50,490(原始);n=1986 | lt_top10_spec_net IC_OOS +0.43;lt_retail dir−1 OOS +0.69 | 三大額 net 與 champion corr 0.60–0.65(冗餘鏡像)、散戶殘差數學上=−前十大 net;basis 線性/z60 OOS 皆負 IC≈0 perm p 0.80–0.89;連 champion 自身 DSR 0.593 fail | 動能偽裝/冗餘鏡像,非獨立 alpha。落 **L2** 但不採信,僅同義佐證顯示欄 |
| **大戶持股集中度** 千張大戶/集保 | FinMind `TaiwanStockHoldingSharesPer`(逐級距 people+percent) | 506,702(28,880 stock-week、164 檔) | 三子訊號(千張大戶Δ/z/合成集中) | 全 null:IC≈0、去動能 partial IC 歸零翻負(corr mom 0.21–0.23)、perm p 0.26–0.77、DSR 全 fail;多頭段 IC 反為負;與 champion corr −0.075 正交 | 動能偽裝/無效(乾淨 null)。落 **L2**,至多當籌碼背景。**部分**:僅 164 大型股(>1M 級距被外資保管行稀釋),中小型股(真主力)+ 2018–22 + 質押待補 |
| **ETF 申贖/受益人數/折溢價** | FinMind `TaiwanStockHoldingSharesPer`(total 列給 people+SHOUT);S2 NAV FinMind 無(422 拒絕) | 2,548(8 檔 ETF 週頻) | hg_all OOS +0.834(唯一 OOS 正) | 不可信:IS IC=−0.012 近噪音、perm p=0.077 未過、曝險 60% 且與 champion 報酬 +0.37(非設計的反向);申贖 flow 完全無訊號;pcMom≈0=乾淨雜訊非污染 | 非真 alpha(S1 flow+S3 holders 大盤層無穩健預測力,乾淨雜訊)。不進生產層。**部分**:S2 折溢價待補(無官方 NAV);下一步方向=個股/高息類股橫斷面非大盤擇時 |

**Phase-2 status 遷移:** 期貨進階/選擇權 由「proxy-borderline」→ **真資料實跑**;融資維持率 由「✅proxy」→ **真資料(推翻 proxy)**;大戶集中度/ETF 由「⛔待接」→ **✅真資料(部分)**;國際總經 由「🟡部分」→ **✅真資料(部分)**。整體 11 維度:**10 已實跑(真資料) / 1 待接(信用微結構)**,其中 4 個標「部分」仍缺子維度。

**Phase-2 三條方法論再確認:**
1. **真資料 > proxy 是硬需求** — 融資維持率 proxy 給了方向相反的尾部結論;不用 ground-truth 會誤採一個反向訊號。
2. **perm 顯著 ≠ alpha** — 選擇權 PCR_OI perm p=0.004 看似鐵證,但 permutation 不控動能共線;DSR+殘差IC 才是照妖鏡(把它殺成動能組合)。
3. **補資料的產出常是「乾淨證偽」而非新 alpha** — 六維度真跑後 champion 依然是唯一領先 alpha;真資料的價值是用高信賴度擋掉偽訊號,不是變出第二支腳。

---

> **⚠️ 非投資建議聲明**
> 本報告為量化研究與方法論記錄,所有結論均為歷史資料的統計觀察,**不構成任何個股、指數或衍生品的買賣建議**。回測績效不代表未來報酬;所有「訊號」在真實交易中的表現受滑價、成本、regime 轉換與存活者偏誤影響。作者非持牌投資顧問,讀者應自行判斷並承擔一切投資風險。
