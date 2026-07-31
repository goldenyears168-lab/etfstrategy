# 整合多閘系統測試 (integrated_multigate) —— 全專案洞見收斂成一個可用系統

_研究員: 量化研究組 (Phase-3, deeper) · 日期: 2026-07-30 · 對齊 chip-macro 方法論_
_腳本: `scripts/research/dashboard/integrated_multigate_study.py`_
_逐日部位/報酬: `data/research/dashboard/integrated_multigate.parquet` · 指標: `integrated_multigate_metrics.csv`_
_verdict: **確認 (champion + tech gate = 唯一可部署核心) + VIX 邊際真增益 + 融資維持率 veto 乾淨 null(結構性冗餘)**_

---

## 0. 一句話結論

把三個各自驗證過的閘門疊到領先籌碼 champion 上,**真資料實跑四組 long/flat 系統**,結論明確:

- **tech gate 是唯一的主力**:champion 單獨 OOS Sharpe +1.81 / maxDD −18.5% / DSR 0.891(**fail**)→ 加 tech gate(close>上彎MA200)後 +2.61 / −3.9% / **DSR 0.995(survive)**。這正是 `tech_regime.md` 記錄的結果,**本輪完整重現**。
- **VIX gate = 邊際但真實的增益**:再加 VIX 60日 z≤2 放行,OOS Sharpe +2.61→**+2.73**、maxDD −3.9%→−3.5%、**DSR 0.995→0.998**。OOS 只砍掉 10 個「tech&champ 都在多但 VIX 噴出」的日子,量級小、方向對、與 champion 幾乎不共線(corr +0.10)→ **非冗餘的弱 veto**。
- **融資維持率<150 veto = 乾淨 null(結構性冗餘)**:D 與 C **完全相同**(Sharpe/maxDD/DSR 每一位數字一致)。原因是機械性的:**當 tech&champ 同時做多時,維持率最低只到 153%,全樣本從未 <150%**。維持率<150 只在 2024-08(日圓套利平倉)、2025-04(關稅崩)這種深尾 flush 觸發,而那時 close 早已跌破上彎 MA200(tech gate 關閉)→ 這個 veto **永遠咬不到系統實際做多的任何一天**。這正好實證 `margin_maintenance.md` 的核心發現:**MR<150 是「空頭 flush 底部 marker」,不是「危險 marker」,與趨勢向上系統正交、放不進去。**

**整體定位**:整合系統的真正產品 = **tech-gated champion(B)**,VIX gate 為可選的邊際強化(C),融資維持率 veto 在此架構下**不應加入**(不是它沒用,而是它的作用域與 bull gate 不相交)。

---

## 1. 這測試是什麼 · 為何是最高價值

前面 16 個維度研究各自證偽 / 驗證單一訊號,結論散落在各報告。本測試把**三個實測站得住的閘門**收斂成一個端到端系統,回答唯一真正重要的工程問題:

> **每多疊一個閘門,是否 _進一步_ 改善 deflation 校正後的系統(DSR / maxDD),還是冗餘 / 反傷?**

四組 long/flat 系統(TAIEX,同一約定、同一成本 4bps/邊):

| 系統 | 定義 | 來源維度 |
|---|---|---|
| **A** champion alone | `fut_foreign_oi z60 > 0` | chip-macro champion(領先籌碼核心) |
| **B** tech-gated | A **且** `close > 上彎MA200` | `tech_regime.md`(L0 regime gate,已知 DSR≈0.995) |
| **C** tech+VIX-gated | B **且** `VIX 60日 z ≤ 2` | `global_macro.md`(VIX 大 spike 恐慌 veto,perm p=0.035) |
| **D** +margin veto | C **且** `維持率 ≥ 150%` | `margin_maintenance.md`(深尾斷頭壓力) |

---

## 2. 訊號與資料(全本地,零外接;逐日 point-in-time 對齊)

| 閘門 | 精確定義 | 方向 | 資料源 | 前視控制 |
|---|---|---|---|---|
| champion | `fut_foreign_oi` 對自身 60 日均值 z-score > 0 | 多 | `panel.parquet` | 專案標準單 bar 約定(全維度一致) |
| tech gate | `close>MA200` **且** `MA200.diff(22)>0`(上彎) | 多 | `panel.parquet` ix OHLC | 同上 |
| VIX gate | `(VIX−MA60)/std60 ≤ 2`(z>2 才 veto) | veto | `global_macro_data.parquet`(Yahoo `^VIX` 真值) | **merge_asof(backward, allow_exact=False)** → TW 日 t 只看到 t 之前最後一筆美盤 VIX(對齊 `global_macro_study`,即產生 p=0.035 的同一對齊) |
| margin veto | `maintenance_pct ≥ 150`(<150 才 veto) | veto | `margin_maintenance_data.parquet`(FinMind 官方整戶擔保維持率真值) | 域內盤後值,同 champion 單 bar 約定 |

- 門檻(z>2、150%、MA200/22日斜率)**全部 a-priori 凍結**,沿用各來源報告已定義的值,**本輪不做任何網格搜尋**(避免 980T / adopted-44 過擬合前例)。
- 覆蓋:VIX z60 **1986/1986**、維持率 **1986/1986**,無缺口。
- IS/OOS 70/30 時間切(IS<2024-02-15,OOS n=596)。方向全 a-priori 多,不偷看 OOS。

---

## 3. 實跑結果(已實際執行,`integrated_multigate_study.py`)

Buy&Hold OOS Sharpe **+1.41**。VIX gate 在 OOS 共 veto 54 天(9.1%),維持率 veto 僅 10 天(1.7%)。

### 3.1 四系統對照(OOS)

| 系統 | exposure | IS Sharpe | **OOS Sharpe** | **OOS maxDD** | perm p | **DSR(nt8)** | DSR(nt16) |
|---|---|---|---|---|---|---|---|
| A champ alone | 0.37 | +0.95 | +1.81 | **−18.5%** | 0.039 | **0.891 fail** | 0.820 |
| **B tech-gated** | 0.32 | +1.14 | **+2.61** | **−3.9%** | 0.000 | **0.995 survive** | 0.988 |
| **C tech+VIX** | 0.30 | +1.01 | **+2.73** | −3.5% | 0.000 | **0.998** | 0.995 |
| D +margin veto | 0.30 | +1.01 | +2.73 | −3.5% | 0.000 | 0.998 | 0.995 |

### 3.2 逐閘增量(核心問題的答案)

| 加入的閘門 | ΔOOS Sharpe | ΔmaxDD | ΔDSR(nt8) | exposure 變化 | 判定 |
|---|---|---|---|---|---|
| **+tech** | **+0.79** | **+14.6pp**(−18.5→−3.9%) | **+0.105**(0.891→0.995) | 0.37→0.32 | **主力**:把 DSR 從 fail 抬過 0.95,砍掉整段空頭尾部 |
| **+VIX gate** | +0.12 | +0.4pp | +0.003 | 0.32→0.30 | **邊際真增益**:小但方向對、非冗餘 |
| **+margin veto** | **+0.00** | +0.00 | +0.000 | 0.30→**0.30** | **乾淨 null**:結構性冗餘,一天都沒動 |

### 3.3 為什麼 margin veto 完全無效(機械性證實)

直接查逐日 parquet:

- OOS 中維持率<150 的 10 天全部落在 **2024-08-05/06**(日圓套利平倉)與 **2025-03-31→04-22**(關稅崩)。
- 這 10 天裡,系統 C 的部位 **全部為 0**(早被 tech/VIX 關掉)。
- **全樣本**中,維持率<150 而系統 C 仍做多的日子 = **0 天**。
- 當 tech&champ 同時做多時,**維持率最低只到 153.0%,全歷史從未跌破 150%**。

→ 融資維持率<150 是**空頭深尾 flush 的底部標記**,它與「close>上彎MA200」的作用域**根本不相交**:flush 發生時 tech gate 已關。這不是「維持率沒資訊」,而是**它的資訊(斷頭底部)對一個只在趨勢向上時武裝的系統毫無邊際貢獻**——完全呼應 `margin_maintenance.md`「MR<150 是 contrarian 底部 marker(fwd20 +2.26%),非 veto marker」的結論。**若真要用維持率深尾,正確用法是逆勢抄底 confirm,不是順勢系統的 veto。**

### 3.4 regime-conditioning(為何 tech gate 這麼有效)

| regime | champion OOS Sharpe | 天數 |
|---|---|---|
| ALL | +1.81 | 596 |
| **bull (>MA200)** | **+2.78** | 526 |
| bear (≤MA200) | +0.26 | 70 |

champion 的 edge 幾乎全在多頭(bull +2.78 vs bear +0.26)。tech gate 的價值 = **把那 70 天幾乎無 edge 的空頭日子關掉**,maxDD −18.5%→−3.9%。這是「砍尾部」而非「加報酬」(呼應 Faber 2007)。

### 3.5 共線 / 冗餘檢定

| 閘門 | corr(champion z60) | corr(60日價格動能) |
|---|---|---|
| tech_ok | −0.24 | **+0.56**(它就是動能定義) |
| vix_ok | +0.10 | +0.11 |
| margin_ok | −0.04 | +0.37 |

- tech gate 與價格動能 corr +0.56 → **它是動能的定義**(下游任何在此 gate 上條件化的籌碼/分點訊號一律需 stage-matched permutation,見已知陷阱)。
- VIX gate 與 champion 幾乎不共線(+0.10)→ **非冗餘**,其 +0.12 Sharpe 是真增量。
- margin gate 與 champion 也近正交(−0.04),但如 3.3 所示其作用域與系統不相交 → 統計不共線 ≠ 有邊際用途。

---

## 4. verdict · lead/lag · 落層 · 與 champion 搭配

| 元件 | verdict | 落層 | 與 champion 的關係 |
|---|---|---|---|
| **tech gate** | **確認 / 過濾(真實、決定性)** | **L0** | 決定 champion 何時允許武裝多單;把 DSR 從 0.891(fail)抬到 0.995(survive),maxDD 砍 14.6pp。**系統能否部署的關鍵單一貢獻。** |
| **VIX gate** | **前兆(弱但真)** | **L0** | 恐慌 spike(z>2)當日 veto;OOS +0.12 Sharpe、DSR→0.998、非冗餘。可選的邊際強化。 |
| **margin<150 veto** | **乾淨 null(結構性冗餘)** | (不放入本系統) | 作用域與 bull gate 不相交,全樣本 0 天生效。維持率深尾的正確用法是**逆勢抄底 confirm**,不是順勢 veto。 |

- **整合系統的 lead/lag**:核心 alpha(champion=外資期貨 positioning)是**領先**;三個閘門全是**同步→落後的 regime/風險背景**,職責是**條件化/否決**領先訊號,不是並列的第二支獨立 alpha 腿。
- **可部署產品** = **B(tech-gated champion)**,C(再加 VIX gate)為邊際加分版。**D 不採用**。

---

## 5. 已知陷阱與最大保留

1. **單一多頭週期過擬合(最大保留,與 tech_regime 同)**:OOS 窗(2024-02→2026-07)596 日中僅 **70 日空頭**。「DSR 0.995→0.998」是在**單一多頭週期**取得,`var_trials` 用保守 1/n 近似。**gated champion 通過 DSR 是強力候選、非定論,須再過一個完整空頭週期方可信。**
2. **margin veto 的 null 是「此窗此架構」的**:2024-08 與 2025-04 兩次 flush 皆為 V 型急殺急彈,tech gate 剛好在低點關閉。若未來出現**緩跌破線 + 維持率溫水煮青蛙**式的空頭,維持率深尾與 tech gate 的作用域可能相交,結論需重驗。**但方向不變:維持率深尾是抄底 marker,強行當順勢 veto 在邏輯上就錯位。**
3. **VIX +0.12 的顯著性上限**:只砍 10 個 OOS 多頭日,樣本極少,ΔDSR +0.003 在 DSR 量測誤差內。**定位為「合理、非冗餘、但不可誇大」的弱 veto**,不宜宣稱獨立顯著。
4. **VIX 美盤時差**:`^VIX` 有 +1 TW 日時差(已用 merge_asof backward 防前視);在地 VIXTWN(TAIFEX 隱波,同日可用)理論上更佳 → **待補**,可能小幅改善 VIX gate 的時效。
5. **與價格動能共線(tech gate corr +0.56)**:任何在 tech gate 上再疊的籌碼/分點訊號必做 stage-matched permutation(branch-follow 已踩坑 p=0.15–0.23)。
6. **成本 / 換手**:B→C exposure 0.32→0.30,換手增量極小,4bps/邊已計入;VIX veto 增加少量進出但淨為正。
7. **champion 一 bar 約定**:champion 與 margin 皆盤後資料,沿用專案全維度一致的單 bar 約定以保四系統可比;VIX 另用 merge_asof 更嚴格對齊。此不對稱是刻意的(可比性 vs 美盤時差),已於腳本註明。

---

## 6. implementable_now 判定

- **本地即可實作、已實跑**:四系統全套(champion + 三閘 + permutation + DSR + regime + 共線 + 逐日 parquet)——三個 parquet 直接跑,零外接。**implementable_now = true。**
- **建議部署**:**B(tech-gated champion)** 為核心;**C(+VIX gate)** 為邊際強化版;**D(margin veto)不採用**(結構性冗餘)。
- **下一步**:①抓在地 VIXTWN 重驗 VIX gate 時效;②待下一個空頭週期做 out-of-regime 複核(最大保留);③維持率深尾若要用,改做**逆勢抄底 confirm**的事件級研究,而非順勢 veto。

---

_本報告為量化研究之證偽性分析,非投資建議。所有訊號僅供研究討論,不構成任何買賣、持有之推薦。回測含前視/過擬合/單週期/資料覆蓋限制;gated champion 通過 DSR 係在單一多頭窗取得,須再經一個完整空頭循環方為定論。過去回測表現不代表未來績效。_
