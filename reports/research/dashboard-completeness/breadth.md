# 市場廣度／騰落 (Market Breadth / Advance-Decline) — 維度研究

**維度分類**：L0/L1 regime 確認層 · **領先/落後定位：同步 (coincident)**
**可否本地實作**：是（`implementable_now = True`，本報告數字為實跑產出）
**腳本**：`scripts/research/dashboard/breadth_study.py`
**產出**：`reports/research/dashboard-completeness/breadth_metrics.csv`
**資料窗**：2018-06-04 → 2026-07-27，1,983 交易日，中位參與家數 1,036 檔（還原股價宇宙）

---

## 1. 這維度是什麼、為何看它

市場廣度衡量「上漲的東西有多廣」，而不只是「指數漲多少」。當指數由少數權值撐住、而多數個股在跌，指數上行是「虛胖」；反之若上漲家數持續壓過下跌家數，趨勢有橫斷面支撐。核心家族：

- **漲跌家數 A/D count**：`adv`、`dec`、`unch`。
- **騰落比 ADR** = `adv/dec`（>1 偏多）。
- **騰落線 ADL** = `Σ(adv−dec)` 累積線（只看與指數的「背離」，不看絕對水位）。
- **中站上均線比例 %>MA50 / %>MA200**：全宇宙收盤 > 自身 MA 的比例，門檻 50%，最像 **L0 regime 燈**。
- **新高新低 NH-NL** = `(#52週新高 − #52週新低)/參與家數`；衍生 McClellan 震盪與 Summation。
- **廣度衝力 Zweig Breadth Thrust (ZBT)**：`adv/(adv+dec)` 的 10 日 EMA 在 ≤10 交易日內由 <0.40 升破 >0.615 → 多頭 regime 觸發（文獻中唯一「帶領先味」的廣度用法）。

專業上看它，是因為廣度是**同一橫斷面價格的再表述**：對「大盤 regime 確認」很直接、便宜、可解讀（breadth-zone、頂背離敘事），因此適合放在 L0/L1 當**確認燈與背離監看**，而非獨立 alpha。

### 學術／實務依據（引用調研 refs）
- **Zaremba, Bianchi, Mikutowski (2021), *Economic Modelling* 97:348-364** —「Herding for profits: Market breadth and the cross-section of global equity returns」。廣度對**橫斷面**（市場/產業組合）未來報酬有穩健正向預測力，穩健於 size/style/vol/momentum/trend，且效應在「多頭後、套利限制高」時最強→**行為性、regime 依賴**。⚠️ 這是 cross-section 預測，**不可**直接搬到 index time-series 擇時（本專案這裡的用途）。
- **Zweig Breadth Thrust（StockCharts ChartSchool）** — 廣度家族中**最具領先根據**的用法，歷史上是多頭確認訊號。
- **Hindenburg Omen（SentimenTrader / QuantifiedStrategies 綜述）** — NH-NL 同時擴張的見頂訊號**偽陽性高達 ~80%**，證明 NH-NL 背離作為前兆極不可靠。
- **GitHub**：`twjackysu/TWSEMCPServer`（TWSE/TPEX/TAIFEX OpenAPI 封裝，官方漲跌證券數對帳基準）；StockCharts/QuantifiedStrategies 提供 McClellan/ZBT/Hindenburg 權威定義（US 市場、多未做嚴謹 OOS/DSR）。**查無**任何對台股廣度做過 point-in-time + OOS + DSR 的成熟開源專案——與 chip-macro 相同的「白地」：人人畫 ADL 圖、無人誠實驗證。

---

## 2. 訊號精確定義（公式／正規化／方向）

**還原股價強制**：A/D 判定用 `stock_close_adjusted.adj_close_v2`（除息+除權還原）。用原始 close 會在除息日製造整批「假跌」→ 汙染家數。

1. `adv_t = #{ret_t>0}`、`dec_t = #{ret_t<0}`，`ret` 為還原股價日報酬。
2. **正規化廣度**（Zaremba 式）`net_breadth = (adv−dec)/(adv+dec) ∈ [−1,1]` —— 除以**參與家數**而非固定股數，吸收上市家數變動與近端 backfill 不全。10 日平滑後取 `z60`。
3. `adr = adv/dec`；`adl = Σ(adv−dec)`（累積 → **非平穩，必 z-score** 才可比）。
4. `pct_above_ma50/200 = 宇宙中 收盤>自身 SMA 的比例`（`pct_ma200_lvl = pct−0.5` 當 L0 門檻燈）。
5. `nhnl = (#52週新高 − #52週新低)/參與家數`，10 日平滑後 `z60`。
6. **Ratio-Adjusted McClellan** `= EMA19(adv_ratio) − EMA39(adv_ratio)`，`adv_ratio = adv/(adv+dec)`。
7. **ZBT event** = `EMA10(adv_ratio)` 由 <0.40 升破 >0.615（≤10 日）→ arm 多頭。

**方向**：由 **IS IC 符號**決定（不看 OOS）。`directed = sign(IC_IS)·z`；`directed>0` → 做多次一日 index（open→close）。
**無未來函數**：第 t 日廣度收盤後才知 → 最早 t+1 open 可交易（`fwd(t)=ix_close[t]/ix_open[t+1]…`，同 panel 慣例）。NH-NL 與 %>MA 逐日以「當時宇宙 + 僅過去 252 日」point-in-time 重算。

### 資料源（全本地，無須接新源）
| 用途 | 來源 | 說明 |
|---|---|---|
| 還原股價（A/D、%>MA、NH-NL） | `data/stocks.db` → **`stock_close_adjusted.adj_close_v2`** | 除息還原，2010-01→2026-07-27，2018 約 870 檔/日 → 2026 約 1,100 檔/日；`cum_factor` 由事件史重建 |
| 大盤 TAIEX（對帳） | `data/stocks.db` → `daily_bars` where `code='IX0001'` | 2015→今 |
| 冠軍 + 前向報酬 | `data/research/chip_macro/panel.parquet` | `fut_foreign_oi` z60、`ix_open/ix_close` |

**選配官方對帳（未使用，僅備援）**：TWSE rwd `https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=MS&response=json`（官方漲/跌/平證券數合計，僅上市）；TWSE OpenAPI `https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX`；上櫃 TPEX OpenAPI。FinMind **無**原生漲跌家數 dataset，需自 `TaiwanStockPrice` 逐檔聚合（本專案已由本地 `stock_close_adjusted` 涵蓋上市+上櫃+興櫃）。

---

## 3. 研究設計（依專案方法論）

- **時序擇時測試**（非橫斷面）：`breadth_t` → 做多 index t+1；成本 `COST_BPS=4`（換手計）。
- **IS/OOS 70/30** 時序切分；IC 只在 IS 定方向（OOS 樣本 n=595）。
- **Permutation vs 同曝險隨機**：固定 #做多日數、shuffle 部位位置，2000 draws，OOS Sharpe 單邊 p。
- **Deflated-Sharpe 式減損**：以掃描變體數 `n_trials=8` 當多重測試校正（`emax=√(2ln N)` 門檻），輸出 `deflated_p`（>0.95 才算穩健）。
- **regime-conditioning**：`bull = ix_close > 上彎 MA200`，分別看 OOS 多頭/空頭 Sharpe。
- **反「動能偽裝」檢定（決定性）**：
  1. `raw_IC_full` = 廣度 level 對前向報酬全樣本 IC；
  2. `resid_IC_ctrl_mom` = 把廣度 level 對「價格短動能 z + 慢趨勢 z」做正交化後，**殘差**對前向報酬的 IC；
  3. `corr_vs_champ` = 廣度策略日報酬與冠軍策略日報酬相關（是否獨立）。
- **ZBT** 以事件研究評估（arm 後 20/60 日前向報酬 vs 無條件基準）。

---

## 4. 實跑結果（2018-06 → 2026-07，OOS n=595）

**基準**：B&H OOS Sharpe **+0.57**；冠軍（外資期貨 z60>0）OOS **+1.11**。

| signal | dir | IC_IS | OOS_Sharpe | corr_vs_champ | raw_IC_full | **resid_IC_ctrl_mom** | OOS_bull | **OOS_bear** | deflated_p | exposure |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| pct_ma200_z60 | +1 | +0.000 | **+1.24** | +0.30 | +0.026 | **+0.013** | +1.65 | **−2.74** | 0.45 | 0.52 |
| pct_ma50_z60 | +1 | +0.016 | +0.78 | +0.34 | +0.017 | +0.006 | +1.13 | −2.02 | 0.20 | 0.50 |
| pct_ma200_lvl | −1 | −0.009 | +0.57 | +0.62 | +0.011 | −0.011 | +1.01 | −1.74 | 0.12 | 1.00 |
| mcclellan | +1 | +0.046 | +0.53 | +0.38 | +0.020 | +0.002 | +0.85 | −2.07 | 0.11 | 0.51 |
| net_breadth_z60 | +1 | +0.062 | +0.23 | +0.35 | +0.025 | +0.012 | +0.32 | −0.55 | 0.05 | 0.47 |
| adr_z60 | +1 | +0.010 | +0.22 | +0.38 | +0.020 | −0.015 | +0.76 | −2.16 | 0.04 | 0.36 |
| nhnl_z60 | +1 | +0.036 | +0.01 | +0.32 | +0.021 | +0.014 | +0.17 | −1.24 | 0.02 | 0.47 |
| adl_z60 | +1 | +0.026 | −0.21 | +0.24 | −0.004 | −0.013 | +0.21 | −1.41 | 0.01 | 0.23 |

**最佳存活者 `pct_ma200_z60`**：OOS Sharpe +1.24，permutation **p=0.027**，但 **deflated_p=0.448（未過 0.95）**。
**冠軍 + pct_ma200_z60 50/50 組合**：OOS Sharpe **+1.47**（冠軍單獨 +1.11）——表面加分。

**ZBT 事件研究**：2018-06→今僅 **2 次觸發**（2020-04-08 COVID 反彈、2021-02-22）。arm 後 fwd20d 平均 +2.77%、fwd60d +9.43%（均高於無條件 +1.61% / +4.97%），方向與文獻一致，但 **n=2 純屬軼事**，無統計力。

### 誠實判讀（這是 Stage-4 `margin_bal` 的翻版）
1. **線性資訊量≈0**：所有訊號 `raw_IC_full` 都在 +0.02 附近，正交化掉價格動能後 `resid_IC_ctrl_mom` 更趨近 0（最佳者僅 +0.013）。Sharpe 幾乎全來自「二元方向閘門的曝險」，不是預測內容。
2. **edge = 多頭 regime 的做多曝險**：每個訊號 **OOS 多頭 Sharpe 強正、空頭 Sharpe 強負**（pct_ma200：+1.65 / −2.74）。這正是「同步 regime 燈」的指紋——它只是在多頭時保持做多。
3. **多重測試不過關**：permutation 表面顯著（p=0.027）但 deflated_p=0.448 遠低於 0.95，掃了 8 個變體後選最高者本就會有這種表面顯著（呼應 980T / adopted-44 過擬合史）。
4. **與冠軍高度同向**：`corr_vs_champ` 0.24–0.62，組合的 +1.47「加分」多來自同一批多頭日的做多，非獨立分散。

**結論**：市場廣度**不是**獨立領先 alpha，而是一盞**同步的多頭 regime 確認燈**。與調研的先驗判定一致（廣度=同一橫斷面價格再表述，對 index 擇時同步、非領先）。

---

## 5. lead/lag 定位、落層、與冠軍怎麼搭

- **領先/落後**：**同步 (coincident)**。對照冠軍（外資台指期 positioning = 真領先，chip×bull OOS +1.79），廣度不具同級領先性，實跑已將其「揭穿為動能偽裝／regime 曝險」。
- **落層**：**L0/L1**。`pct_above_ma200`（>50%）是最乾淨的 **L0 regime 燈**；`net_breadth`/ADR/McClellan 屬 L1 價量確認。
- **與冠軍搭配（確認／過濾／前兆三格）**：
  - **確認（主用）**：冠軍給「該不該站多方」，`pct_above_ma200>50%` 給「多方是否有橫斷面支撐」。**兩者同綠才是高信賴多頭**；冠軍綠但廣度 <50%（少數權值撐盤）= 降級/減碼提示。這是廣度**唯一穩健**的用法，屬 regime 一致性檢查、不新增部位方向。
  - **過濾（弱）**：空頭 regime（MA200 下彎）時廣度全面失效，只能當「別站多方」的附證，不可反手做空。
  - **前兆（僅一格值得續測，門檻高）**：**ZBT** 是廣度中唯一帶領先味者，但本地 8 年僅 2 事件、無統計力——列為「觀察型前兆」，需更長樣本或跨市場擴充才可能採信。**NH-NL 頂背離判為不可靠前兆**（Hindenburg 偽陽性 ~80%），不納入。

**建議整合**：把 `pct_above_ma200`（連續值 + 50% 燈）與 `net_breadth_z60`（背離監看）併入 regime 日報／dashboard 的 **L0 一致性列**，與冠軍並排顯示「方向一致 / 背離」，**不**作為獨立進出訊號。

---

## 6. 已知陷阱與規避

| 陷阱 | 本研究如何規避 |
|---|---|
| **與價格動能共線（最大陷阱）** | 對 level 做「短動能 z + 慢趨勢 z」正交化，只看殘差 IC → 證實殘差 edge≈0（動能偽裝坐實） |
| **同步非領先／事後選樣背離** | 明確定位同步；ZBT 以事件研究、NH-NL 背離判為不可靠 |
| **前視／倖存者偏差** | NH-NL、%>MA 逐日 point-in-time（僅過去 252 日 + 當時宇宙），非用今日宇宙回貼 |
| **近端 backfill 不全**（07-28/29 僅 46 檔） | 設 `MIN_PARTICIPANTS=600` 門檻，剔除未完整同步日；並用「參與家數」正規化 |
| **除息假跌** | 強制 `adj_close_v2` 還原股價 |
| **漲跌停鎖死扭曲家數** | 用 net_breadth（比例）而非絕對數，降低少數鎖死股影響；後續可加流動性加權 |
| **多重測試灌水** | permutation + deflated_p（n_trials=8 校正）+ IS 定方向，不採信單一表面顯著 |
| **regime 依賴** | 明確分 OOS 多頭/空頭 Sharpe，揭露 edge 只在多頭 |

---

## 7. 可續作（誠實標註優先度）
- **低優先／已足夠**：廣度本身作為獨立 alpha ——已證偽，不建議續投。
- **中優先**：把 `pct_above_ma200` + `net_breadth` 背離接入 regime 日報 L0 一致性列（純確認燈，工程小）。
- **觀察型**：ZBT 續蒐事件（跨更長樣本／MTX 全市場），維持 event-study 框架、不落地為部位訊號。
- **需接新源才做**：官方 TWSE `MI_INDEX` 漲跌家數對帳（選配，非阻塞）；橫斷面 Zaremba 版廣度（產業組合選股，屬不同用途，需另開 cross-section 框架）。

---

*本報告為研究記錄，非投資建議。所有數字由 `scripts/research/dashboard/breadth_study.py` 於本地 `stock_close_adjusted` + `panel.parquet` 實跑產出，可重現。廣度經 IS/OOS + permutation + Deflated-Sharpe + regime 條件化 + 動能正交化後，判定為同步 regime 確認燈，非獨立領先 alpha。*
