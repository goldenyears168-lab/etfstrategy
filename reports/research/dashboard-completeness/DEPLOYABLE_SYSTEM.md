# 可部署系統規格 — 籌碼→大盤擇時 × 基本面選股 (DEPLOYABLE_SYSTEM)

_研究員: 量化研究組 (Phase-4 硬化) · 日期: 2026-07-31 · 對齊 chip-macro 方法論_
_定位: **風控 overlay + 選股層規格書**,供實盤 / daily_tracker 使用。非投資建議。_
_來源證據: `integrated_multigate.md` (L0 擇時系統) · `tej_fundamental_factor.md` (L1 選股) · `daily_tracker.py` (現行 tracker)_
_本文件 **只記錄與建議,未改動任何 tracker 邏輯**。_

---

## 0. 一句話

可部署核心 = **System B/C = 領先籌碼 champion(外資期貨 z60>0)× tech gate(close>上彎MA200)[× VIX gate(60日z≤2)]**,OOS Sharpe +2.68 / maxDD −3.8% / **DSR 0.995→0.998**;上面疊 **L1 rev_yoy_3m 月營收動能** 做選股(OOS DSR 0.967,PIT 乾淨)。**現行 daily_tracker 是這套系統的近似,但 tech gate 用 MA150-stage 而非驗證過的 MA200-上彎,量級較弱(見 §4 差異記錄)。**

---

## 1. 系統分層總表

| 層 | 職責 | 訊號 | 門檻(凍結) | 方向 | 驗證狀態 |
|---|---|---|---|---|---|
| **L0-a 籌碼 champion** | 市場今天能不能站多方 | 外資期貨淨未平倉 `fut_foreign_oi` 對自身60日均 z-score | **z60 > 0** | 多 | 領先 alpha;單獨 DSR 0.891 **fail**(肥尾) |
| **L0-b tech gate** | 允許 champion 武裝的趨勢背景 | `close > MA200` **且** `MA200.diff(22) > 0`(上彎) | 兩者皆真 | 過濾 | **決定性**:B 系統 DSR **0.995 survive** |
| **L0-c VIX gate** | 恐慌 spike 否決 | 美盤 `^VIX` 對自身60日均 z-score | **z60 ≤ 2** 放行(>2 veto) | veto | 邊際真增益,C 系統 DSR **0.998**;perm p=0.035 |
| **L1 選股** | 站多方時抱哪些個股 | 月營收 YoY 近3月均 `rev_yoy_3m` 橫斷面排序 | 多前20% / 空後20%,週再平衡 | 多空 | **真 alpha(有保留)**,OOS DSR **0.967** |
| (不採用) 融資維持率 veto | — | 整戶擔保維持率<150 | — | — | **乾淨 null(結構性冗餘)**:與 bull gate 作用域不相交,全樣本 0 天生效 |

**部位公式(System C)**：`position = champion_green AND tech_ok AND vix_ok`(long/flat,1 或 0)。
**選股(L1)**：在 position=1 的日子,於 rev_yoy_3m 前段個股配置(獨立層,不需 champion 當閘門——空頭仍 +0.72)。

---

## 2. 訊號精確定義 / 門檻 / 資料源 / 更新頻率

### L0-a 籌碼 champion（領先核心）
- **定義**: `z60 = (fut_foreign_oi − MA60(fut_foreign_oi)) / std60(fut_foreign_oi)`;**綠燈 = z60 > 0**。
- **資料源**: FinMind `TaiwanFuturesInstitutionalInvestors`(外資台指期未平倉),入 `data/research/chip_macro/panel.parquet` 欄位 `fut_foreign_oi`。
- **更新頻率**: 每日盤後(TAIFEX ~15:00 後 FinMind 更新);tracker 於傍晚 refresh。
- **前視控制**: 專案標準單 bar 盤後約定(全維度一致)。
- **保留**: champion 的 edge 幾乎全在多頭(bull OOS Sharpe +2.78 vs bear +0.26)→ 必須配 tech gate。

### L0-b tech gate（決定性過濾,DSR 從 fail 抬過 0.95）
- **定義（驗證版 = System B）**: `close > MA200` **且** `MA200.diff(22) > 0`(200日均線上彎)。
- **資料源**: `panel.parquet` 的 `ix_close`(TAIEX 收盤)。
- **更新頻率**: 每日盤後。
- **與價格動能共線 corr +0.56** → **它就是動能定義**;任何在此 gate 上再疊的籌碼/分點訊號必做 stage-matched permutation(branch-follow 已踩坑 p=0.15–0.23)。

### L0-c VIX gate（弱但真的恐慌 veto）
- **定義**: 美盤 `^VIX` 的 `z60 = (VIX − MA60)/std60`;**z60 > 2 當日 veto**(不重新武裝多單),z60 ∈ (1.5, 2] 為邊際警戒(黃燈)。
- **資料源**: Yahoo `^VIX` 日線(tracker `load_vix()` 直抓)。
- **更新頻率**: 每日;**美盤 +1 TW 日時差**,研究用 `merge_asof(backward, allow_exact=False)` 對齊,即 TW 日 t 只看到 t 之前最後一筆美盤 VIX。
- **保留**: OOS 只砍 10 個多頭日,ΔDSR +0.003 在量測誤差內 → 定位「合理、非冗餘、不可誇大」;在地 VIXTWN(同日可用)理論更佳，**待補**。

### L1 選股 rev_yoy_3m（月營收動能,唯一過 DSR 的橫斷面因子）
- **定義**: 每檔取「最新公告」月營收 YoY%,近3個月均 = `rev_yoy_3m`;90 檔大型股每日排序,**多前20% / 空後20%,等權,每5日(週)再平衡**,t 成訊 t+1 進場,成本 4bps/邊×週轉。
- **資料源**: TEJ `TWN/EWSALE`（`d0003` YoY% + `annd_s` 公告日）;報酬用 TEJ `close_adj`（除權息完全還原）。
- **更新頻率**: 月營收台股每月10日前公告 → **週再平衡**足夠。
- **⚠️ 月營收 PIT 鐵律(已遵守)**: study 用 `annd`（公告日）gate,只 ffill 公告日 ≤ t 的最新值 → **無前視**。切勿把營收綁在營收月當月使用。
- **不需 champion 當閘門**: 加 champion×多頭 gate 反而把 Sharpe +2.34 降到 +1.53(空頭仍 +0.72,是獨立選股層)。
- **不要合成**: composite(rev+roe+pb)OOS +0.88 遜於純 rev_yoy_3m +2.34;PB 台股反向(OOS −0.96),ROE 乾淨 null。**單用 rev_yoy_3m**。

---

## 3. 實跑真數字（本輪 Phase-4 於 panel.parquet 2018-06→2026-07-29 重跑驗證）

OOS 切分 IS<2024-02-15（與 integrated_multigate 一致）。長/平,champion=z60>0 二值,隔日報酬,4bps 已於原研究計入。

| L0 系統 | OOS Sharpe | OOS maxDD | exposure | 對照報告值 |
|---|---|---|---|---|
| champion 單獨(A) | — | — | 0.37 | 報告 +1.81 / −18.5% / DSR 0.891 **fail** |
| **champ × MA200-上彎(驗證版 B)** | **+2.68** | **−3.8%** | 0.318 | 報告 +2.61 / −3.9% / **DSR 0.995** ✅重現 |
| champ × MA150-stage（**tracker 現行 L0**） | **+2.17** | **−8.7%** | 0.289 | 無 DSR 憑證(非驗證口徑) |
| tech+VIX(C) | — | −3.5% | 0.30 | 報告 +2.73 / **DSR 0.998** |

**L0 caliber 一致性**: MA150-stage 與 MA200-上彎 逐日訊號 **agreement 88.1%**（1787 有效日中 213 日分歧;exposure 0.701 vs 0.725）。

**L1 rev_yoy_3m**（90 檔,2021-01→2026-07,週再平衡）: OOS IC +0.101、OOS Sharpe +2.34、perm p=0.002、**DSR 0.967**、Bonferroni 0.012、空頭仍 +0.72、corr_champion +0.27。

**當前燈號(2026-07-29 收盤)**: ix 40,039;MA150 37,330 / MA200 34,858(皆站上且上彎);tsmom126 +0.30;**armed=True / tech200=True**;**champion z60 = −1.15 → 紅燈** → **系統 C 部位 = 0（空手）**。趨勢環境武裝,但外資期貨未確認(籌碼 risk-off)。

---

## 4. daily_tracker 與 System C 的差異記錄（**核心交辦**）

`scripts/research/chip_macro/daily_tracker.py` 是本系統的每日執行器,已具 champion + MA150 stage + VIX gate + 融資維持率燈。經逐行比對,**它是 System C 的近似,但有三處口徑偏差**,依交辦僅記錄與建議,**未改動**:

| # | 項目 | tracker 現行 | 驗證版 System C | 影響 | 建議 |
|---|---|---|---|---|---|
| **1** | **tech gate 口徑** | L0 = `close>MA150` **且** `MA150.diff(25)>0` **且** `tsmom126>0`(Weinstein stage 風格) | tech = `close>MA200` **且** `MA200.diff(22)>0`(上彎) | **實質**:OOS Sharpe +2.17 vs **+2.68**、maxDD −8.7% vs **−3.8%**。DSR 0.995 憑證屬 **MA200 口徑**,MA150 口徑無 DSR 背書 | 若要對齊已驗證 DSR,tracker L0 應改用 MA200-上彎;或明確標註「tracker 用較敏感 MA150 stage,量級數字勿套 DSR 0.995」 |
| **2** | **VIX gate 未進部位** | `position = armed × size`(見 `compute()` L202);VIX 僅作**監控燈**顯示,**未** veto 部位 | System C 把 `vix_ok` AND 進 position | tracker headline 部位 = System **B**(非 C);VIX spike 日 tracker 仍可能顯示做多 | 屬保守設計(VIX 邊際+0.12、樣本薄);若要對齊 C,需把 vix_ok 併入 pos。現狀可接受,但應記為「headline=B,VIX 為 advisory」 |
| **3** | **融資維持率燈休眠** | `main()` 呼叫 `build_dashboard(panel, night, vix=vix)` **未傳 margin** → 維持率燈預設不亮 | System D(維持率 veto)本就**不採用**(乾淨 null) | 無負面影響——正確方向(維持率 veto 對順勢系統結構性冗餘) | 維持現狀;若要顯示維持率作**逆勢抄底 confirm**(非 veto),再另接 |

**綜合判定**: tracker 的訊號家族、方向、風控哲學與 System C **一致**;唯一實質須留意的是 **#1 tech gate 用 MA150 而非 MA200**——這使 tracker 的部位比驗證版略敏感(exposure 略低、maxDD 較深),且其量級**不繼承 DSR 0.995 憑證**。這是「口徑差異」非「錯誤」,但供實盤解讀時務必知悉。

---

## 5. 更新頻率 / 資料源總表（運維用）

| 元件 | 資料源 | 頻率 | tracker 抓取點 | 缺料 fallback |
|---|---|---|---|---|
| champion `fut_foreign_oi` | FinMind 期貨法人 OI | 日盤後 | `build_panel.main()` | panel.parquet→csv |
| tech gate `ix_close` | TAIEX 收盤(panel) | 日盤後 | panel | csv |
| VIX gate `^VIX` | Yahoo Finance | 日 | `load_vix()` | 不亮此燈 |
| 夜盤 TX(L3 參考,未驗證) | FinMind `TaiwanFuturesDaily` | 日夜盤後 | `fetch_night_bias()` | 略過 |
| L1 rev_yoy_3m | TEJ EWSALE `d0003`+`annd_s` | 週(月營收月頻) | 選股層,非 tracker | 待補批次 |
| 融資維持率(休眠/逆勢用) | wantgoo −ETFA / FinMind | 日 | `load_maintenance()` | 本地 csv 快取 |

---

## 6. 已知保留（**部署前必讀**）

1. **單一多頭週期過擬合(最大保留)**: OOS 窗 2024-02→2026-07 共 596 日中僅 **70 日空頭**。DSR 0.995→0.998 是在**單一多頭週期**取得,`var_trials` 用保守 1/n 近似。**gated champion 通過 DSR 係強力候選、非定論,須再過一個完整空頭週期方可信。** 同理 L1 rev_yoy_3m OOS 僅 ~1 循環且偏多頭(TEJ 2021+ 硬限)。
2. **DSR 脆弱性**: champion 單獨 headline 報酬**肥尾**、DSR borderline fail;整套系統的 DSR 全部依賴 tech gate 砍尾部(maxDD −18.5%→−3.9%)這一機制。若 tech gate 在「緩跌破線+溫水煮青蛙」式空頭失效,DSR 憑證需重驗。
3. **margin veto 的 null 是「此窗此架構」**: 2024-08 與 2025-04 兩次 flush 皆 V 型急殺急彈,tech gate 剛好低點關閉;若未來出現緩跌破線+維持率溫水,維持率深尾與 tech gate 作用域可能相交。**但方向不變:維持率深尾是逆勢抄底 marker,強當順勢 veto 邏輯錯位。**
4. **tech gate = 動能定義(corr +0.56)**: 任何在此 gate 上再疊的籌碼/分點訊號必做 stage-matched permutation。
5. **L1 為已知因子**: revenue/earnings momentum(PEAD 族)非新發現;價值在「TEJ 真資料在台股 90 檔仍活著」,宇宙僅 90 檔大型股。
6. **VIX 美盤時差 + 弱顯著**: +1 TW 日時差(已 merge_asof 防前視);ΔDSR +0.003 在量測誤差內,不可誇大為獨立顯著。
7. **tracker 口徑偏差(§4)**: MA150 vs MA200、VIX 未進部位——實盤解讀時勿把 tracker 部位量級直接套 System C 的 DSR。

---

## 7. 監控清單（daily / weekly）

**每日(盤後,tracker 自動)**
- [ ] champion z60 是否翻正/翻負(部位開關的主觸發)。當前 −1.15 紅燈。
- [ ] close 對 MA200 與 MA200 斜率(tech gate 是否關閉=砍尾部觸發)。當前站上且上彎。
- [ ] VIX z60 是否 >2(恐慌 spike advisory);當前需查。
- [ ] 部位翻轉日記錄於 `signal_history.csv`。

**每週**
- [ ] L1 rev_yoy_3m 排序再平衡(月營收公告後);確認全用 `annd` 公告日 gate(無前視)。
- [ ] champion exposure 與 buy&hold 對照(系統定位=風控,近8年總報酬 x1.78 < B&H x3.66,用放棄上漲換 Sharpe +1.31>0.94、maxDD −10% vs −32%)。

**每月 / 每季**
- [ ] 追蹤 OOS 是否終於納入一段**真空頭**(最大保留的解除條件)。
- [ ] champion 訊號衰減檢查(60日 z 的 hit-rate 滾動)。
- [ ] 若接維持率:改做**逆勢抄底 confirm** 事件級(MR<150 深尾 fwd20 +2.26%),**非**順勢 veto。

**觸發警示(需人工複核)**
- champion 翻多 **但** VIX z60>2:tracker 現狀會顯示做多(headline=B),System C 會 veto → 人工判斷是否降/暫停武裝。
- 緩跌破 MA200 且維持率溫水下滑:§6.3 的作用域相交情境,tech gate/維持率結論須重驗。

---

_本報告為量化研究之證偽性分析與工程規格,非投資建議,不構成任何買賣、持有之推薦。所有回測含前視/過擬合/單一多頭週期/資料覆蓋(TEJ 2021+、panel 2018+)限制。gated champion 與 rev_yoy_3m 通過 DSR 係在單一多頭窗取得,須再經一個完整空頭循環方為定論。過去回測表現不代表未來績效。_
