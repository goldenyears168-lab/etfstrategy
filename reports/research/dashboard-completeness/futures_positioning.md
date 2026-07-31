# 期貨進階籌碼:大額交易人 OI + 台指期正逆價差 (basis)

**維度**: 期貨 / 選擇權 / 信用槓桿 area → 「大額交易人未平倉(前五/十大)」與「台指期 basis」兩個子訊號
**定位**: L2 籌碼核心層的擴充,對照 champion(外資台指期 positioning,`fut_foreign_oi_z60>0`)
**狀態**: `implementable_now = True`(兩訊號皆**非**本地表 / 非 panel 欄,但用專案標準 FinMind client 即可直拉;本研究已 end-to-end 跑通、真資料落地 `data/research/dashboard/futures_positioning_data.parquet` 並得真數字。非 SCAFFOLD)
**結論**: **兩訊號皆不採信** — OOS 皆不勝 champion(+1.12)、`corr_vs_champ` 皆 >0.4(非獨立)、**DSR 皆 <0.95**(retail 0.34)、combo/veto 對 champion 無加成;大額 net 是外資 champion 的冗餘鏡像,basis 線性為負、極端反轉近 0
**verdict**: **動能偽裝 / 冗餘鏡像**(非真 alpha、非獨立前兆)——大額 net 與 champion 共線 0.60–0.65 且是外資的鏡像;basis 是價格動能的同步影子
**資料真實性**: FinMind `TaiwanFuturesOpenInterestLargeTraders`(5,965 列)+ `TaiwanFuturesDaily`(44,525 列),涵蓋 **2018-01-02→2026-07-30**;台指期三大法人 OI(champion)取自既有 panel
**腳本**: `scripts/research/dashboard/futures_positioning_study.py`(`--with-external` 讀快取跑完整框架;`--refresh` 才重拉 FinMind,省配額)
**日期**: 2026-07-30

---

## 1. 這維度是什麼、專業為何看它

現有 panel 的期貨籌碼只有「三大法人」口徑(`fut_foreign_oi/fut_trust_oi/fut_dealer_oi`)。本維度補兩塊 panel 完全沒有的資料:

- **訊號 A — 大額交易人未平倉**:TAIFEX 每日盤後(~15:00)公布「期貨大額交易人未沖銷部位結構」,含**前五大 / 前十大**交易人的買方、賣方未沖銷口數與佔比,並區分**「十大特定法人」**(法人)與「近月契約 vs 所有契約」兩版。台股在地共識把「十大特定法人淨部位」當主力方向代理。這與三大法人口徑**不同**:大額報表是按「單一交易人部位大小」切,不是按身分別切。
- **訊號 B — 台指期正逆價差 (basis)**:`basis = TX 近月期貨價 − TAIEX 現貨`。正價差(期>現)=市場願意付溢價持有多單=偏多情緒;逆價差(期<現)=避險 / 看空。carry / roll-yield 是跨資產文獻中穩定的期望報酬預測因子。

**專業為何看**:兩者都試圖回答「大額 / 槓桿資金的方向與情緒」,理論上比純現貨買賣超更前瞻(期貨是保證金槓桿、reference 未來)。但本專案方法論要求證偽優先——先假設它們**只是 champion 的冗餘**或**價格動能的偽裝**,除非通過門檻才採信。

### 學術 / 實務依據(引用本維度調研 refs)

- **Wang (2025), Journal of Futures Markets** — 台指期法人情緒 / positioning 的**低頻分量**最具預測力(wavelet + Markov)。呼應本專案 Stage5:大額 / basis 訊號應做**低頻分解 + regime 分層**,而非 raw level-z。
- **Macrosynergy, "Equity Index Future Carry"** — basis / carry 標準操作化 = `(F−S)/到期時間`,**必須做股利與展期季節調整**;carry 係數穩定正、顯著。→ 台股 basis 必先去除權息季節。
- **J. Commodity Markets (2021), LME basis study** — 多數 futures-return predictor 同號預測 spot,但 **basis 是例外**、有獨特角色 → basis 分量可能提供與其他籌碼因子**不同號**的資訊,值得測獨立性。
- **Sanders/Irwin/Leuthold (SSRN 39932)** — 情緒 / 散戶對期貨價**整體無系統性偏誤,僅「極端水位」才有預測力** → 支持把大額殘差(散戶)與 basis 做成**極端分位**訊號而非線性 level。
- **Economic Modelling (2014, TVP-VAR) / IRFE (2011)** — 台指期部位訊號線性無效、**預測力近年遞減**、僅極端 imbalance 才有訊號 → 量級保守。
- **GitHub / 在地實務**:`FinMind/FinMind`(兩 dataset 現成 SDK 直拉);量化通 / newbie168 VBA 爬蟲——把前十大特定法人 net 當領先訊號**疊價判讀,但無回測 / 門檻 / 正規化 / OOS**,典型「主張未驗證」。本研究補上證偽驗證。

---

## 2. 訊號精確定義(公式 / 正規化 / 方向)

house 慣例:訊號取 `close[t]`、隔日 `ix_open[t+1]→ix_close[t+1]` 成交(no-lookahead);大額報表 15:00 才出,最早 t+1 開盤可交易,符合約定。方向由 **IN-SAMPLE IC 符號**固定(不偷看 OOS)。正規化用 `rz(x,60)=(x−mean60)/std60`。

### 訊號 A:大額交易人未平倉

原始欄位(前十大特定法人・近月契約):`net = top10_specific_long_oi − top10_specific_short_oi`。三個衍生量:

| 代號 | 公式 | 預期定位 |
|---|---|---|
| `lt_top10_specific_net_z60` | `rz(net_top10_specific, 60)`,方向 +1 | 領先(但與 champion 同格,高度共線) |
| `lt_retail_residual_z60` | `rz(市場總 net − 前十大 net, 60)`,方向由 IS IC 定(預期 −1 反指標) | **前兆 / 反指標**(最值得測的獨立候選) |
| `lt_6to10_net_z60` | `rz(前十大 net − 前五大 net, 60)` = 第 6~10 大(較快錢) | 弱領先 |

其中 `lt_retail_residual` = 「小額交易人淨部位」= `市場總 OI net − 前十大 net`,散戶反向情緒代理。另可測 `lt_net_minus_foreign`(大額 net − 外資期貨 net)= 去掉外資的本土大戶 / 自營殘差。

### 訊號 B:台指期 basis

```
basis      = TX_近月_close − ix_close            # 點數
basis_pct  = basis / ix_close                    # 比率
carry_ann  = basis_pct * 252 / days_to_expiry    # 年化 carry(roll yield)
```

**去季節(關鍵)**:6–8 月除權息旺季→期貨機械性提前扣息→大逆價差,這是**日曆不是訊號**。正確做法扣理論持有成本得 fair-value,殘差 = 定價偏誤 / 情緒分量:

```
fair_basis   = ix_close * (r_f − q_div) * days_to_expiry / 365   # r_f 無風險利率, q_div 預期股利率
basis_resid  = basis − fair_basis                                # 情緒分量
```

簡化近似版(較不乾淨)= `rz(basis_pct, 60)`,靠 60 日均去掉緩慢季節趨勢。**方向不可假設,兩派須實測**:動能派(逆價差=續弱,同步/落後)vs 反轉派(極端逆價差=避險超賣→回彈,前兆)。在地共識:線性 level 無方向性,只有**極端分位 × regime** 才有訊號 → 主訊號設計為 `basis_resid < p10 分位 & bull_regime` 的尾部反轉,而非線性 level。

**展期**:近月每月第 3 週三結算,basis→0 收斂;剩 ≤3 交易日轉次月(或加權),避免收斂假訊號污染。

---

## 3. 資料源

| 訊號 | 本地 | 需接資料源 | dataset / endpoint |
|---|---|---|---|
| A 大額交易人 | **無**(DB 無表) | FinMind(免 token 可拉,600/hr) | `TaiwanFuturesOpenInterestLargeTraders`(data_id=`TX`, 1998-07+, 每日 16:30);官方 = TAIFEX `largeTraderFutQry` |
| B basis 期貨腳 | **無**(panel 無 TX 價;`futures_institutional_daily` 只有 OI 無價) | FinMind | `TaiwanFuturesDaily`(data_id=`TX`, close/settlement_price/open_interest/contract_date) |
| B basis 現貨腳 | **有** | — | panel `ix_close`(TAIEX, 2018-06→2026-07-29) |
| 去季節利率 / 股利率 | 無 | FinMind `TaiwanStockTotalReturnIndex` / TWSE 殖利率;或 FRED | 簡化版可先跳過,用 `rz(basis_pct,60)` 近似 |

原始 TAIFEX OpenAPI 備援:`largeTraderFutQry`(大額)、`DailyMarketReportFut`(每日期貨行情)。

---

## 4. 研究設計(依專案方法論)

比照 `scripts/research/chip_macro/eval_stage4_newhypotheses.py` + `eval_deflated_sharpe.py` house style,證偽優先:

1. **IS/OOS 70/30 時間分割**;方向由 IS IC 符號固定,OOS 完全 held-out。
2. **no-lookahead 對齊**:訊號 `close[t]` → 成交 `ix_open[t+1]→ix_close[t+1]`(大額報表 15:00 出,shift 已保守)。
3. **permutation vs 同曝險隨機**:固定相同「做多天數 k」隨機重排,檢定 OOS Sharpe 是否勝隨機(而非只勝 0)。
4. **Deflated-Sharpe**:計入搜尋次數(scan + 分位門檻)、skew/kurt 厚尾修正;預期在多重測試 + 厚尾下 DSR **幾乎必 <0.95**,頭條 Sharpe 打折。
5. **regime-conditioning**:`bull = ix_close > 上彎 MA200`;預期 edge 僅多頭出現,空頭明示無可靠 edge。
6. **獨立性 / 共線雙檢**(本維度核心):
   - `corr_vs_champ`:與 champion 每日策略報酬相關;**<0.4 才算獨立**新來源。
   - `corr_mom20`:與 20 日價格動能相關 + 動能 partial-out 殘差 IC;若殘差 IC≈0 = **籌碼是動能偽裝**(Stage4 融資教訓,70% 是動能)。
7. **與 champion 搭配測試**:50/50 combo、champion 被 basis / 散戶殘差 gate / veto 後的 OOS Sharpe。

---

## 5. lead_lag 定位 + 落層 + 與 champion 搭配

對照 champion(外資台指期 positioning = **領先**, chip×bull OOS +1.79):

| 子訊號 | lead_lag 格 | L 層 | 與 champion 關係 | 實測結果(第 6 節) |
|---|---|---|---|---|
| 大額前十大特定法人 net | **領先**(但同格) | L2 | **冗餘**——外資即最大宗 | 確認:corr_champ 0.63,OOS 0.43<champ,冗餘 |
| **大額散戶殘差**(總−前十大) | 前兆 / 反指標(先驗) | L2 | 原以為互補 | **推翻**:數學上 = −(前十大 net),corr_champ 0.60,非獨立 |
| 大額 net − 外資(本土大戶殘差) | 弱領先 | L2 | 口數小、雜訊高 | 代理(投信+自營 OI)corr_champ 0.69,冗餘 |
| basis 原始 level | **同步** | L1↔L2 | 期現近乎同時定價 | 確認:OOS −0.42,線性無方向性 |
| basis 去季節殘差(極端分位) | 前兆(僅極端值,先驗) | L2 | 極端逆價差 × bull 或領先反彈 | **未成立**:OOS −0.20,曝險 6.4%,無穩定前兆 |

**綜合定位**:兩者都**不太可能是獨立於外資期貨 champion 的新 alpha**。最有機會通過門檻的是①**大額散戶殘差反指標(前兆)**與②**去季節 basis 極端分位 × bull regime(前兆)**——兩者皆須先證明 `corr_vs_champ<0.4` 且勝純價格動能,再談採信。

**與 champion 的搭配方式**(非取代):
- **確認(confirm)**:champion 做多時,若大額散戶同時極端偏空(反指標對齊)→ 加碼信心。
- **前兆 / 過濾(precursor / filter)**:champion 空手期間,basis 極端逆價差 × bull → 反彈前兆的**戰術性進場**;或散戶極端偏多時對 champion 多單做 **veto**。
- champion 仍是主腳,本維度是**分散來源 / 情緒過濾層**,不是新的獨立 alpha 腳。

---

## 6. 實跑結果(真實數字,end-to-end)

基準(panel 2018-06→2026-07-29, n=1986, OOS n=596):`B&H OOS = +0.39`;`champion OOS = +1.12`(exposure 0.37);`bull_regime frac = 0.65`。

### 6a. 訊號 A — 大額交易人(FinMind `TaiwanFuturesOpenInterestLargeTraders`, TX 近月, 2018→2026)

| 子訊號 | dir | IC_IS | OOS Sharpe | corr_vs_champ | corr_mom20 | perm_p | 判定 |
|---|---|---|---|---|---|---|---|
| `lt_top10_spec_net_z60`(前十大特定法人 net) | +1 | **+0.101** | +0.43 | **+0.63** | −0.05 | 0.27 | 冗餘 |
| `lt_retail_net_z60`(散戶殘差反指標) | −1 | −0.086 | +0.69 | **+0.60** | −0.06 | 0.12 | 冗餘 |
| `lt_t6_10_spec_net_z60`(第 6~10 大) | +1 | +0.033 | +0.37 | **+0.65** | −0.17 | 0.32 | 冗餘 |

**識別性洞察(資料本身告訴我們的)**:市場淨部位恆為 0(每口多對一口空)→ **散戶殘差 net = −(前十大 net) 是數學恆等式**,不是獨立資訊,只是大戶方向的鏡像;方向翻轉後 OOS 看似最高(+0.69)但 `corr_vs_champ=+0.60` 仍與 champion 共線。三者 `corr_vs_champ` 皆 **>0.4**(0.60–0.65)= **印證調研**「外資即最大宗特定法人,大額 net 與 champion 重疊、非獨立」。`lt_top10_spec_net` IC_IS +0.101 是全研究最高單一 IC,但那是因為它幾乎**就是** champion,OOS perm_p=0.27 不顯著、OOS Sharpe 反而低於 champion。

### 6b. 訊號 B — basis(FinMind `TaiwanFuturesDaily`, TX 近月 position 時段 close − `ix_close`)

| 子訊號 | dir | IC_IS | OOS Sharpe | corr_vs_champ | corr_mom20 | perm_p |
|---|---|---|---|---|---|---|
| `basis_pct_raw`(線性 level) | −1 | −0.012 | **−0.42** | +0.56 | +0.25 | 0.80 |
| `basis_pct_z60`(去季節近似) | −1 | −0.011 | **−0.56** | +0.45 | +0.15 | 0.89 |
| `basis_extreme_low × bull`(極端逆價差反轉) | — | — | **−0.20** | — | — | (exp 6.4%) |

**解讀**:線性 basis level **OOS 為負**、IC≈0、perm_p≈0.8–0.9 = **印證在地與學界共識「線性 basis 無方向性」**。house 認為唯一可能有機會的「極端逆價差 × bull 反轉」在本樣本也是 **−0.20**、樣本僅 6.4% 曝險 = 無穩定前兆 alpha。註:此處 z60 為去季節「近似」,未扣 fair-value(需利率 / 股利率);basis 期貨腳用 `TaiwanFuturesDaily` 一般交易時段(position)近月 close,非結算價(settlement_price 僅結算日有值),但既然線性方向與極端反轉都不成立,精緻去季節 / 換結算價不會翻盤結論。

### 6c. Deflated-Sharpe(真實計算,非定性宣稱)

DSR 用 house 風格:`var_trials` 由本研究實測的 8 個候選 OOS Sharpe 離散度估(std daily SR≈0.029),`n_trials=29`(8 候選 + 21 保守 pad 涵蓋未揭露窗 / 門檻變體),`SR*_expected_max ≈ +0.96 年化`。

| 策略 | 年化 OOS Sharpe | skew | kurt | PSR(vs 0) | SR*(年化) | **DSR** | 判定 |
|---|---|---|---|---|---|---|---|
| champion `fut_foreign_oi` | +1.12 | −0.31 | 23.3 | 0.95 | +0.96 | **0.593** | FAILS |
| `lt_retail_net_z60`(最強大額候選) | +0.69 | −0.60 | 15.0 | 0.85 | +0.96 | **0.341** | FAILS |

**關鍵誠實揭露**:即使是 champion 本身,在計入搜尋膨脹 + 厚尾(kurt 23)後 **DSR=0.593 也 <0.95**——這與 chip-macro 母研究「headline z60 Deflated-Sharpe FAILS」結論一致(見 MEMORY)。大額散戶殘差 DSR=0.34 更低。**沒有任何一個本維度訊號通過 DSR**。

### 6d. 與 champion 搭配(confirm / veto,真實計算)

| 組合 | OOS Sharpe | 對 champion |
|---|---|---|
| champion alone | **+1.12** | 基準 |
| champion ⊕ retail 50/50 combo | +0.97 | **稀釋**(corr 0.60,拉低) |
| champion ÷ retail 極端偏多 veto | +1.07 | 幾乎無作用(僅 veto 1.0% 交易日) |

50/50 combo 因兩腳高共線(0.60)只是稀釋 champion;散戶極端 veto 一年只觸發約 1% 天數、反而略降。**搭配測試證實:大額訊號對 champion 沒有加成**。

### 6e. 驗證管線佐證(panel 內代理,live 段)

`live_baseline` 另用「非外資本土法人期貨 OI」代理(投信+自營 OI, `corr_vs_champ=+0.69`)獨立佐證共線陷阱真實存在——與 6a 大額實測同向,交叉確認結論非偶然。

**總結**:兩訊號都**可實作、已實測、皆不採信**。無一勝 champion,無一 `corr_vs_champ<0.4`,無一通過 DSR,搭配 champion 無加成。這是乾淨的證偽結果,與調研先驗預測完全一致。真資料已持久化至 `data/research/dashboard/futures_positioning_data.parquet` 供重用。

---

## 7. 已知陷阱與規避

1. **basis 季節性(最大陷阱)**:6–8 月除權息機械逆價差=日曆非情緒。不去股利就是在交易「夏天」,回測出現無經濟意義規律(無前視但偽)。**規避**:`basis_resid = basis − fair_basis`(扣持有成本)或至少 `rz(basis_pct,60)` + 只用極端分位 + 標註季節。
2. **共線 A(大額 vs champion)**:前十大特定法人由外資主導。**規避**:必算 `corr_vs_champ`,只採信殘差(散戶=總−前十大)。
3. **共線 B(價格動能)**:basis 與大額 net 隨大盤同向漂移(Stage4 融資 70% 是動能)。**規避**:`corr_mom20` + 動能 partial-out 殘差 IC。
4. **避險污染**:大額 / 特定法人含現貨套利、選擇權對沖腿,net 未必是方向性看法,報表無法拆。**規避**:定性標註 net 可能誤導,優先用「散戶殘差」而非法人 net。
5. **前視 / 延遲**:大額報表 15:00 才出→只能 t+1;basis 即時價勿用當日收盤預測同日收盤。**規避**:一律 `shift(-1)` 成交。
6. **結算收斂 / 展期跳空**:近月到期 basis→0。**規避**:剩 ≤3 日轉次月。
7. **DSR / 過擬合**:厚尾+多重測試下 DSR 幾乎必 <0.95。**規避**:計入搜尋次數、量級保守、不宣稱 standalone alpha。
8. **regime 依賴**:空頭失效。**規避**:bull/bear 分層,空頭明示無 edge。
9. **報表定義變動**:特定法人分類、近月 vs 所有契約口徑歷年調整。**規避**:長史對齊、優先近月契約。

---

## 8. 落地建議

實測後**不建議把這兩訊號當交易 alpha 進 panel / 系統**——它們不勝 champion 且非獨立。若仍要生產化,價值只在**監控 / 敘事**用途,建議:

1. (可選)`build_panel.py` 加拉 `TaiwanFuturesDaily` TX 近月價,新增 `tx_near_close / basis / basis_pct` 欄——basis 是好的**盤面情緒儀表**(給人看的日報數字),但非擇時因子;避免每跑重拉。
2. (可選)`TaiwanFuturesOpenInterestLargeTraders` 前十大特定法人 net 作為 champion 的**同義佐證**顯示欄,不作獨立訊號。
3. 若要更嚴謹否證 basis 前兆:接利率 / 股利率做 fair-value 去季節後再測極端分位——但第 6 節線性 + 近似 z 皆否,翻盤機率低,優先度低。
4. 重跑本研究:`python scripts/research/dashboard/futures_positioning_study.py --with-external`(FinMind 直拉並快取到 `reports/research/dashboard-completeness/cache/`)。

---

*本報告為量化研究方法論記錄,非投資建議。作者非持牌投資顧問;所有訊號均為統計研究性質,未經前瞻實盤驗證,不構成任何買賣建議。*
