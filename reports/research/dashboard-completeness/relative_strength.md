# 相對強度 RS(個股 vs 大盤 TAIEX)— 維度研究報告

分層定位: **L1 價量層(個股相對市場位階)** ｜ lead_lag: **同步偏落後(momentum-class)**,唯 Weinstein RS 背離具窄用途「前兆」性質
狀態: **implementable_now = True**(本地資料齊備,本報告數字為實跑結果)
研究腳本: `scripts/research/dashboard/relative_strength_study.py`
輸出指標: `reports/research/dashboard-completeness/relative_strength_metrics.csv`

---

## 1. 這維度是什麼 / 專業為何看它

相對強度 RS = 個股走勢相對大盤(加權指數 TAIEX)的強弱。最原始形式是 Dorsey RS 線:
`RS_line = 個股收盤 / 大盤收盤`,>1 且上揚代表跑贏大盤。台股在地工具(XQ、籌碼K線、jiafar)的「A/B 相對比值」即此。

專業上看 RS 的三個理由:
1. **選強汰弱**:同一多頭波段,強勢股先發、續航長;RS 是最直接的「這檔在不在領先群」量尺。
2. **突破確認**:Weinstein Stage Analysis 要求「底部型態突破時 RS 必須同步轉正/上彎」才算有效突破 —— RS 當**確認濾網**而非獨立擇時。
3. **背離預警**:大盤下跌時個股 RS 逆勢走揚 = 相對大資金在承接,可為後續突破的**前置訊號**(窄用途)。

### 學術 / 專業依據(本次調研引用)

| 來源 | 洞見 | 對本研究的用法 |
|---|---|---|
| Weinstein 1988《Secrets for Profiting in Bull and Bear Markets》(Mansfield RS 公式,見 stageanalysis.net / ChartMill) | `RSM = (RS_line / SMA(RS_line,52w) − 1)×100`,零線=52週MA;突破須配 RS 上彎。 | **首選正規化形式**,與專案既有 Weinstein Stage-2 個股框架直接相容(見 `src/stage_analysis.py`)。本研究以日線 n=50 實作。 |
| George & Hwang, *Journal of Finance* 2004,《The 52-Week High and Momentum Investing》 | `nearness = price / 52週高` 的預測力優於過去報酬,且**長期不反轉**。 | 最抗反轉的 RS 操作化,對台股短期反轉體質特別合適,作為對照組。 |
| Jegadeesh & Titman 1993,《Returns to Buying Winners and Selling Losers》 | 標準 12-1 動能:過去12月報酬、skip 最近1月避短期反轉,買前10%/賣後10%。 | RS 動能對照組,用來檢驗「RS 是不是就是動能」。 |
| *Revisiting the momentum effect in Taiwan: The role of persistency*, JAPWEF 2023 (ScienceDirect S0927538X23000094) | **台灣是動能著名反例**:persistency 低,僅約50%贏/輸家續強,≥25%反轉為 contrarian。 | 證偽先驗:預設天真高-RS 跨截面在台股會失效或反向,必須長窗+skip 近月+多頭 regime。 |
| GitHub `skyte/relative-strength` | IBD 式 RS 百分位(季報酬加權後對 universe 取 percentile)的 Python 參考實作;自述 price API 未 split-adjusted 會出錯。 | IBD 排名對照組;提醒台股除權息還原盲點(見第 5 節)。 |

---

## 2. 訊號精確定義(公式 / 正規化 / 方向)

四種操作化,全部 PIT(僅用到 t 日(含)以前資訊),每日對流動性 universe 做橫截面排名,多空前後五分位:

1. **Mansfield RS 振盪子(Weinstein,首選)**
   `RS_line = adj_close / ix_close`;`RSM50 = (RS_line / SMA(RS_line, 50) − 1) × 100`
   方向:RSM50 越高越強 → 做多。以 50 交易日 MA 去趨勢,是**唯一去掉價格 level 共線的形式**。

2. **RS 12-1 動能**:`RS_line(t−21) / RS_line(t−252) − 1`(skip 近 21 日規避短期反轉)。

3. **George-Hwang 52週高 nearness**:`price / rolling_252d_max`(原始價,最抗反轉)。

4. **IBD 式季加權 RS 排名**:`2·r(0..63d) + r(63..126) + r(126..189) + r(189..252)`,再對當日全 universe 取百分位。

方向一律「訊號越高 = 越強 = 做多」;規則沿用專案:**僅在多頭 regime(指數 > 上彎 MA200)採信**。

### 資料源(全部本地,無需接新源)

| 項目 | 本地表 / 檔 | 備註 |
|---|---|---|
| 個股還原價 | `data/stocks.db → stock_daily_bars`(`source='finmind'`,`adj_close`) | 流動性 universe 的 adj_close ≈99% 有值;**全表僅約10%有值**(數千檔冷門/下市股 + yfinance 列缺 adj),故務必以 finmind + 流動性過濾。 |
| 大盤 TAIEX | `data/stocks.db → daily_bars where code='IX0001'`(close) | 2015-01→今,PIT 乾淨。`panel.parquet` 的 `ix_close` 僅 2018-06 起;DB 內 `TAIEX` code 只有 2026-07 起(不可用於歷史)。 |
| Champion 對照 | `data/research/chip_macro/panel.parquet`(`fut_foreign_oi` z60>0) | 用於跨層去相關檢定。 |

若要擴充:個股還原價可用 FinMind `TaiwanStockPriceAdj` 回填缺口;類股 RS 需 TWSE OpenAPI 產業別指數。皆非必要。

---

## 3. 研究設計(依專案方法論)

- **橫截面多空**:每日按訊號排序,做多前 20% / 做空後 20%,等權,每 5 日(週)再平衡;t 日收盤成訊,t+1 進場(無前視);成本 4bps/邊 × 週轉。
- **IS/OOS**:時間序 70/30 分割(IS ≈ 2018-06→2024,OOS ≈ 2024→2026-07)。
- **Rank IC**:訊號_t 對次週個股報酬的 Spearman 相關(逐日平均)。
- **Permutation**:每日把訊號在個股間洗牌(維持同 #多/#空 曝險),1000 次,對 OOS Sharpe 取單尾 p。
- **regime-conditioning**:指數 > 上彎 MA200(多頭)vs 其餘(空頭)分別算 Sharpe。
- **去相關(反「動能偽裝」核心檢定)**:RS 多空日報酬 vs (a) 個股自身 12-1 動能多空報酬、(b) champion 外資期貨擇時日報酬。

---

## 4. 實跑結果(本地 90 檔流動股,2018-06-01→2026-07-29,1985 交易日)

基準:等權 universe B&H Sharpe(全期)= **+1.46**;個股自身 12-1 動能多空 OOS Sharpe = **+0.77**。

| 訊號 | IC_IS | IC_OOS | Sharpe_IS | Sharpe_OOS | Sharpe_多頭 | Sharpe_空頭 | corr_自身動能 | corr_champion |
|---|---|---|---|---|---|---|---|---|
| **mansfield_rsm50** | −0.018 | +0.038 | +0.441 | **+1.616** | +1.684 | **−0.687** | **+0.359** | −0.109 |
| ibd_rs_rank | +0.019 | +0.042 | +0.998 | +1.195 | +1.584 | −0.690 | **+0.907** | +0.096 |
| gh_52w_high | −0.011 | +0.031 | −0.236 | +0.844 | +0.717 | −0.808 | +0.071 | −0.262 |
| rs_mom_252_21 | +0.032 | +0.035 | +1.110 | +0.774 | +1.289 | −0.053 | **+1.000** | +0.139 |

Best 訊號 `mansfield_rsm50` OOS **permutation p = 0.002**(顯著:擊敗同曝險隨機組合)。

### 誠實解讀

1. **Mansfield RSM 是四者中唯一「不是動能偽裝」的形式**:與自身動能相關僅 +0.36(IBD +0.91、RS動能 +1.00 幾乎就是動能因子本身),且與 champion 相關 −0.11(正交)。這符合理論 —— RSM 以 RS 線對其 50 日 MA 去趨勢,剝掉了價格 level 共線。
2. **但這不是 standalone alpha**:
   - **IC 近乎零**(RSM IC_IS 甚至 −0.018,IC_OOS +0.038):線性橫截面預測力微弱,Sharpe 幾乎全來自五分位尾端 + 多頭 regime,不是穩定單調關係。
   - **強烈 regime 依賴**:多頭 Sharpe +1.68,空頭 **−0.69**;OOS 窗(2024→2026)本身是強多頭,OOS>IS 的漂亮數字有「集中單一多頭波段」嫌疑 —— 與專案反覆踩到的 adopted-44 / Stage-2「edge 集中單一 2026 多頭、無跨 regime」同一模式。
   - **未過 Deflated-Sharpe 門檻**:本研究搜了 4 個訊號 + 參數(n、skip、分位),permutation 只控制曝險不控制 regime timing 與多重測試;比照 chip-macro champion「headline 過 permutation 卻 DSR borderline fail」前例,不宜宣稱獨立 alpha。
3. **台股反轉體質已被驗證**:天真的 RS 動能(rs_mom_252_21)OOS 僅 +0.77 = 跟自身動能一樣、且空頭 Sharpe −0.05 幾乎無效,印證 JAPWEF 2023「台灣動能弱/反轉」的先驗。

**結論**:RS 是**多頭 regime 下的 L1 個股強弱確認濾網 / breadth 元件**,不是市場擇時或獨立選股 alpha。首選形式為 Mansfield RSM(去趨勢、與動能低共線、與 champion 正交)。

---

## 5. lead_lag 定位 + 落層 + 與 champion 怎麼搭

- **lead_lag = 同步偏落後(momentum-class)**:RS 線本質是相對價格,與個股價格同步生成、無資訊領先性,結構同動能(=已走完的落後確認)。對照專案:champion 外資台指期 positioning = 領先;現貨買賣超 = 同步;融資 = 落後動能代理 —— **RS 與融資同類(落後動能代理),但落在個股橫截面而非市場時序**。
- **落層 = L1 價量**:補的是「個股相對市場的位階/強弱」,與 L2 籌碼核心、L0 regime 不同層,可正交組合。
- **窄用途前兆**:僅 Weinstein「RS 在價格突破前先上彎/指數下跌時 RS 逆勢走揚」可當突破前置確認,須多頭 regime,且須另證其領先於個股自身價格突破。

### 與 champion(外資期貨 positioning)的搭配 —— 三種角色

1. **不同層正交疊加(推薦)**:champion 決定「市場今天能不能站多方」(L0/擇時,z60>0),RS 決定「站多方時抱哪些個股」(L1/選股)。實跑 corr(RSM, champion) = −0.11 ≈ 正交,是乾淨的兩層分工。
2. **RS 當 champion 的下游濾網**:僅在 champion=risk-on 的日子,才在 RSM 前段的個股佈局;champion=risk-off 時 RS 空頭 Sharpe −0.69 印證應退場 —— champion 正好補上 RS 缺的市場擇時。
3. **不可替代 champion**:RS 無市場擇時領先性,不能拿來預測大盤方向;它只在 champion 已判定多頭後細化個股選擇。

---

## 6. 已知陷阱與規避

| 陷阱 | 說明 | 本研究如何規避 |
|---|---|---|
| **台股動能反例(最大陷阱)** | JAPWEF 2023:台灣 persistency 低、≥25% 反轉,天真高-RS 恐失效/反向。 | 長窗 + skip 近 21 日 + 鎖多頭 regime;證偽先驗;實跑確認天真 RS 動能確實弱。 |
| **動能偽裝(共線)** | 原始 RS 線 corr(RS, 報酬)極高,edge 其實來自個股自身 Stage-2 趨勢。 | 每個 RS 因子報酬對「自身 12-1 動能」與「champion」做去相關;僅採信低共線的 Mansfield RSM(+0.36)。 |
| **前視 / 資料延遲** | IBD 橫截面百分位需當日全 universe 快照,易誤用未來成分。 | t 日收盤成訊、shift(1) 後才計報酬、t+1 進場;permutation 逐日洗牌。 |
| **股利調整盲點(專案級)** | `stock_daily_bars.adj_close` 全表僅約10%有值(冷門股 + yfinance 列),除權息日 RS 會被未還原價扭曲成假訊號(與 MEMORY「incomplete dividend adjustment」一致)。 | 僅取 `source='finmind'` 流動性 universe(adj ≈99%);缺口可用 FinMind `TaiwanStockPriceAdj` 回填。 |
| **regime 依賴** | RS 只在多頭有效、空頭失效,單獨無擇時能力。 | 分 regime 報 Sharpe,明列空頭 −0.69;定位為多頭濾網而非 alpha。 |
| **搜尋過擬合** | n、skip、分位切點皆可搜,易 IS 過擬合。 | 標註未過 DSR、OOS>IS + 近零 IC 的 regime 集中風險,不宣稱 standalone alpha。 |
| **除權息/分割還原錯誤(GitHub 實作坑)** | skyte 自述 price API 未還原會出錯,台股除權息同理。 | 用還原價 adj_close,非原始 close。 |

---

## 7. 後續可做(非本次範圍)

- 把 Mansfield RSM 併入既有 Weinstein Stage-2 進場閘門(`src/stage_analysis.py` 已有 stage 分類),測「Stage-2 × RSM>0 且上彎」相對「Stage-2 only」的增量。
- 正式 Deflated-Sharpe(比照 `scripts/research/chip_macro/eval_deflated_sharpe.py`),把 4 訊號 × 參數搜尋計入多重測試懲罰。
- 類股 RS(需 TWSE OpenAPI 產業別指數),測「個股 vs 類股 vs 大盤」三層相對強度。
- champion×RS 兩層組合的正式聯合回測(僅 champion risk-on 日 + RSM 前段個股)。

---

*本報告與所附腳本為量化研究記錄,非投資建議,不構成任何買賣或持有特定證券之推薦。歷史回測結果不保證未來績效。*
