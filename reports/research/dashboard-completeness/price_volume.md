# 量價結構(成交值 / 量能 / 帶量突破)— Price-Volume Structure

維度層級:**L1 價量層**(L0 regime → **L1 價量** → L2 籌碼核心 → L3 微結構)
分類定位:**同步(coincident)為主 / confirmation 濾網用途,非領先**
狀態:**implementable_now = True**(大盤層與個股層皆用本地資料實跑完成)

研究腳本:`scripts/research/dashboard/price_volume_study.py`
機器可讀輸出:`reports/research/dashboard-completeness/price_volume_metrics.csv`
資料:`data/research/chip_macro/panel.parquet`(ix_money 大盤成交值)、`data/stocks.db` 表 `stock_daily_bars`(個股 amount)

---

## 1. 這維度是什麼、專業為何看它

「量」是價格之外市場留下的第二條足跡。專業把成交值/量能操作化成因子,不用原始量,而是**相對自身近期水位的正規化**(這與本專案 champion `fut_foreign_oi_z60` 相對 60 日水位完全同構)。用途分三層:

1. **水位/異常量正規化**:量比(當日量 / N日均量)、Abnormal Trading Volume(對過去 20–60 日均值取殘差或 z-score)。
2. **量價一致性(confirmation,本訊號真正的用途)**:突破帶量——突破 N 日高點時要求量 > 均量的一定倍數才算「有效突破」,否則視為假突破;把量當**二值濾網**疊在價格突破上,而非獨立 alpha。
3. **量價背離(前兆情境)**:價創新高但量縮 = 多頭背離警訊。屬情境判讀,連續因子化後可回測性差。

專業看它的核心理由:**量確認價的可信度**。趨勢在「知情資金進場(帶量)」時比「無量飄漲」更可靠。它回答的是「這根突破/這段趨勢可不可信」,而不是「明天漲不漲」。

### 學術 / GitHub 依據
- **Datar, Naik & Radcliffe (1998), *JFM* 1(2):203-219** — 換手率(成交股數/流通股數)當流動性代理,低換手→高預期報酬。提醒:量要操作化成「相對水位」,但這是**月頻橫斷面選股**因子,方向(負向)與時間窗和日頻大盤擇時完全不同,不可直接搬。
- **European Journal of Finance (2024), *Persistence or Reversal? Abnormal Trading Volume***(10.1080/1351847X.2024.2303092) — 異常放量後短線先延續、隨後反轉回歸均值;放量贏家跌得更快。直接支持下文「短期反轉污染」陷阱。
- **Granville (1963), On-Balance Volume** — 唯一把量歸為「領先」的主流論述,主張「量先於價」,但依賴**量價背離情境**(價平量增)而非量水位。本地大盤層級未見穩定領先 IC。
- **Abnormal Trading Volume, Stock Returns and Momentum Effects**(ResearchGate 254659901)— 動能在低量股更強更持久,高量贏家跌更快;佐證量與動能的共線與交互複雜。
- **TA-Lib / pandas-ta** — 標準庫已內建 OBV、AD、ADOSC、CMF、MFI、VWAP、PVT,可免造輪子把量價操作化成連續因子,取 z-score 後套本專案 IS/OOS+permutation 框架。**未找到對台股大盤成交值因子做過 IS/OOS+DSR 嚴謹驗證的公開 repo**(與 chip-macro 同況:nobody OOS-validated),故自建。

---

## 2. 訊號精確定義(公式 / 正規化 / 方向)

令 `M_t` = 當日成交值(大盤 `ix_money`,個股 `amount`),`C_t` = 收盤,`ret1_t = C_t/C_{t-1}-1`。
去趨勢必要:成交值受價格水位/權值結構漂移影響長期向上,**一律正規化**(0 值視為資料缺口→NaN)。

| 訊號 | 公式 | 方向(由 IS IC 定,無 OOS peek) |
|---|---|---|
| `vol_z20` / `vol_z60` | `(M_t − MA_w(M)) / SD_w(M)`,w=20/60 | 由 IC 符號決定(實測弱負) |
| `vol_ratio20`(量比) | `M_t / MA20(M)` | 同上 |
| `obv_slope_z60` | OBV=`Σ sign(ret1)·M`;取 20d 斜率再 z60 | 由 IC 定(實測正) |
| `pvt_slope_z60` | PVT=`Σ ret1·M`;取 20d 斜率再 z60 | 由 IC 定(實測正) |
| `vp_divergence`(量價背離) | `sign(mom5)·(−vol_z20)`:價漲量縮為高 | 由 IC 定 |
| **帶量突破** | `breakout = (C_t ≥ 20d/60d high) AND (vol_z20 > θ)`,θ∈{0,0.5,1} | 二值濾網,非連續因子 |

前瞻報酬對齊(**無前視**):訊號於 `close[t]` 定義,交易 `ix_open[t+1] → ix_close[t+1]`;`fwd5 = C[t+5]/open[t+1]−1`。量在收盤前不完整,嚴禁用當日量做當日訊號。

### 資料源
- **大盤層:本地即足,無需接新源。** `panel.parquet` 已含 `ix_money`(TAIEX 成交值,2018-06→2026-07,零缺值,僅 31 個 0 值為交易日缺口已轉 NaN)。
- **個股層:本地即足。** `stock_daily_bars` 有 `volume` 與 `amount`,2010→2026,2504 檔(2024 起 amount 覆蓋約 73 萬列)。
- **需接新源者(僅換手率/VCP 延伸,scaffold 已備於腳本尾註)**:換手率需流通股數 → FinMind `TaiwanStockInfo`(股本)/`TaiwanStockPrice`(`Trading_Volume`);TWSE `FMTQIK`(大盤成交量值,交叉校驗)、`STOCK_DAY`、`MI_INDEX`;TPEX 對應櫃買統計。

---

## 3. 研究設計(依專案方法論)

1. **IS/OOS 分割**:時間序 70/30(IS=前 70%,OOS=後 30%),方向只用 IS IC 符號固定,OOS 完全不 peek。
2. **Permutation**:vs **同曝險隨機**——保持相同做多天數但隨機挑日,2000 次,取 `null ≥ actual` 比例為 p 值。
3. **Deflated-Sharpe**:對最佳量能策略做 PSR vs `n_trials` 個 null 的期望最大 Sharpe(含 skew/kurt 修正),門檻 0.95。
4. **Regime-conditioning**:只在多頭(指數 > 上彎 MA200,`MA200 > MA200.shift(20)`)測 confirmation 效果。
5. **動能共線控制(核心)**:把 `vol_z20` 對 `mom20` 殘差化,比較 raw IC 與 resid IC——若殘差後 IC 塌到 ~0,即證實「量是動能偽裝」。並在帶量突破中直接對照**同一突破的低量子樣本**(vol vs low-vol 的 fwd 報酬)。

---

## 4. 實跑結果(本地資料,已跑)

基準:B&H OOS Sharpe **+0.39**;champion(外資期貨 positioning)OOS **+1.12**。

**(a) 量能因子掃描(方向由 IS 固定)**

| 訊號 | dir | IC_IS | IC_fwd5 | corr_mom20 | OOS Sharpe | corr_vs_champ |
|---|---|---|---|---|---|---|
| pvt_slope_z60 | +1 | +0.025 | +0.040 | **+0.782** | +0.95 | +0.34 |
| obv_slope_z60 | +1 | +0.056 | +0.057 | **+0.643** | +0.71 | +0.36 |
| vp_divergence | −1 | −0.054 | −0.020 | −0.246 | +0.15 | +0.44 |
| vol_z20 | −1 | −0.027 | +0.002 | +0.254 | −0.64 | +0.45 |
| vol_z60 | −1 | −0.022 | +0.027 | +0.319 | −0.95 | +0.45 |

看似最佳的 `pvt_slope_z60`/`obv_slope_z60` **與 20 日動能相關 0.64–0.78**——是動能偽裝。原始量水位(`vol_z20/z60`)方向弱負、OOS 為負,**單獨量能對大盤幾乎零方向預測力**(IC_fwd5≈0),證偽成立。

**(b) 帶量突破 vs 無量突破(confirmation 的直接檢定)**

| 門檻 | 帶量突破 fwd5(n) | 低量突破 fwd5(n) | 任意突破 | 全樣本 |
|---|---|---|---|---|
| vol_z>0.0 | +0.517% (338) | +0.519% (115) | +0.491% | +0.308% |
| vol_z>0.5 | +0.494% (252) | +0.547% (201) | +0.491% | +0.308% |
| vol_z>1.0 | +0.486% (156) | +0.534% (297) | +0.534% | +0.308% |

**關鍵**:突破本身有超額(+0.49% vs 全樣本 +0.31%),但**帶量突破並未勝過低量突破**(門檻越高甚至略輸)。→ 大盤層級突破的超額來自**突破/趨勢本身**,量的加權**沒有增量**。

**(c) 動能共線控制**:raw corr(vol_z20, day_ret)=**+0.0196** → 殘差化後(對 mom20)=**+0.0135**。本就接近 0,殘差後更小——量無獨立方向資訊。

**(d) Regime + champion 整合**:
- 多頭 regime 帶量突破 long/flat:OOS Sharpe **+0.76**,曝險 12.8%,但 **permutation p=0.064**(未過 5%)。
- champion 單獨 OOS +1.12 vs champion × 量未枯竭濾網 OOS **+1.11**——**量濾網不改善 champion**。
- 最佳量能策略 `pvt_slope_z60` **Deflated-Sharpe = 0.474 → FAILS**(SR* 期望最大 +0.54 > 實際 +0.52)。

**(e) 個股層帶量突破(exploratory,257 檔流動性池,60 日新高)**:帶量突破 fwd10 **+2.0%** vs 低量突破 **+1.7%**——**方向對(帶量 > 無量)但幅度僅 0.3pp**,且含生存者偏誤(退市股缺席、兩邊皆被灌水)。屬「多頭+個股突破」情境下量作為 confirmation 的邊際、非乾淨證據。

---

## 5. lead_lag 定位 + 落層 + 與 champion 怎麼搭

- **lead_lag = 同步(coincident)/ confirmation**。實測 `vol_z20` 與**同日** |報酬| 顯著相關(活動度/波動同步),但對**次日**報酬 IC≈0——量同步反映當日活動度,不含未來方向資訊,不具領先性。對照 champion(外資期貨 OI 對 h=10 仍有 +0.18 IC 的領先結構),量沒有這種結構。唯一可歸「前兆」的是**量價背離**(價漲量縮),但需人工操作化、可回測性差(本測 `vp_divergence` OOS 僅 +0.15)。
- **落層 = L1**。量能是驗證 L0 regime 與價格突破「可信度」的 confirmation 層,不進 L2 訊號層。呼應既有教訓:原始量能濾網曾傷 chip edge(+1.66→+1.00),LMSW「量只在知情時確認」→ L1 併入籌碼層、不用雜訊量。
- **與 champion(領先)的搭配方式**:
  1. **確認(confirm)**:當 champion 發多頭訊號時,帶量突破可作為「這段多頭有量支撐」的加分註記(定性儀表板用),但本測顯示它**不加 Sharpe**,故只作展示不作加碼。
  2. **過濾(filter)**:champion × 量未枯竭(vol_z>−1)只否決極枯量日——實測 +1.11 vs +1.12,**幾乎無差**,不建議用量去 gate champion。
  3. **前兆(precursor)**:量價背離(價創高量縮)可當**多頭轉弱的觀察前兆**,與分點賣超廣度並列 L1/前兆格,但需事件化人工判讀,非連續 alpha。

**結論一句話**:量價結構是 L1 同步 confirmation 層——大盤層級單獨無獨立 alpha(IC≈0、帶量突破不勝無量突破、DSR fail),它的價值是「驗證趨勢可信度」的儀表板註記與量價背離前兆,而非可交易訊號;不改善 champion,也不應加碼。

---

## 6. 已知陷阱與規避

1. **與價格動能共線(最大陷阱)**:本測 corr(vol_z20, mom20)=+0.25、pvt/obv 高達 0.64–0.78。→ 必用 permutation vs 同曝險隨機 **+ 對動能殘差化**才能宣稱增量;本測殘差後 IC 塌到 0.0135,證實多為動能偽裝。
2. **無方向 → 過擬合切點**:量單獨 IC≈0,任何「放量做多」正報酬極可能是門檻在樣本內湊出。→ 已做 IS/OOS + DSR;`pvt_slope_z60` DSR=0.474 FAILS,如預期。
3. **短期反轉污染**:放量後短線先延續、隨後反轉;順勢做多在反轉段吃虧。→ 方向/horizon 敏感,已用 fwd1/fwd5 多窗檢視。
4. **前視/延遲**:盤中量收盤前不完整;嚴格 t 收盤定訊號、t+1 開盤執行;TWSE/TPEX 盤後統計有公布延遲。
5. **成交值口徑漂移**:金額受價格水位/權值結構影響長期向上,**必須正規化去趨勢**(已用 z-score/量比);個股用量(股數)或換手率較穩。
6. **regime 依賴**:空頭放量常是恐慌殺盤,順勢邏輯反轉;已做 regime-conditioning(僅多頭測)。
7. **生存者/股利調整(個股層)**:個股掃描用 `COALESCE(adj_close, close)` 且受退市股缺席影響,結果標為 exploratory,不作採信依據——與本專案既有 blind spot 一致。

---

*本報告為量化研究記錄,所有結論以證偽為先、資料為憑。非投資建議;不構成任何買賣要約或推薦。*
