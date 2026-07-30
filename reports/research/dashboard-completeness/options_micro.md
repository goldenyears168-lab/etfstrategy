# 選擇權籌碼維度（Options Microstructure）研究

維度：**臺指選擇權 P/C ratio、Max Pain、選擇權大額/法人買賣權淨部位、小台散戶多空比**
狀態：**已實跑（全維度真資料）**——S1/S2/S3/S4 四訊號皆跑完真實 IS/OOS + permutation + Deflated-Sharpe + 殘差-IC 共線控制。TXO 全史（2018-01→2026-07，2085 日）PCR_OI/PCR_vol/Max Pain 已抓並回測。
日期：2026-07-30　·　對照 champion：外資台指期 positioning `fut_foreign_oi_z60>0`
腳本：`scripts/research/dashboard/options_micro_study.py`
資料：`data/research/dashboard/options_micro_data.parquet`（合併四訊號 + fwd return，供重用）、`pcr_maxpain.parquet`（PCR/MaxPain 全史）
**verdict：動能偽裝（momentum disguise）為主 · S1 為 champion 的選擇權回聲（弱確認/加權），無獨立 alpha**

---

## 1. 這維度是什麼、專業為何看它

選擇權市場把「方向 + 槓桿 + 時間」壓在同一張報表上，是台股少數能直接讀到**知情資金 positioning** 與**散戶情緒擁擠度**的地方：

- **P/C ratio**——put/call 的相對熱度，散戶恐慌與避險需求的鏡子（反指標型情緒代理）。
- **Max Pain**——到期結算被「釘」向賣方最省錢履約價的微結構效應。
- **法人/大額交易人買賣權淨部位**——大戶用高槓桿合約表達方向信心，與外資期貨 positioning 同構，是四者中唯一的**領先**候選。
- **小台/微台散戶多空比**——用「市場−法人」反推散戶淨部位，擁擠度的落後代理、極端值當反向前兆。

**學術/實證依據（本維度調研引用）：**
- Pan & Poteshman (2006, *RFS*)：個股選擇權「買方開倉」P/C 具私有資訊，低 PCR 股票隔日跑贏高 PCR（週>1%）；槓桿越高預測力越強——支持「大額/法人買賣權淨」= 領先。
- Ni, Pearson & Poteshman (2005)：到期日收盤 clustering 到履約價（19% vs 非到期 18%），確認但**微小**，且主因造市商 delta-hedge 而非操縱——Max Pain 只能當到期週弱先驗。
- 台灣業界（MacroMicro / 國泰期貨 / 豐雲）：台指 PCR_OI 與大盤同期相關約 +0.48 且**落後約 14 天**（= 同步偏落後）；小台/微台散戶多空比為標準反指標，微台（TMF，2022 上市）門檻更低、更純散戶。
- GitHub 生態：TXO PCR/MaxPain 計算腳本常見，但**幾乎無人做過 OOS + permutation + Deflated-Sharpe 的證偽驗證**——與本專案 chip-macro 一致的空白。

---

## 2. 訊號精確定義、正規化、方向

所有訊號皆對自身 60 日做 z-score（對齊 champion `fut_foreign_oi_z60` 慣例），方向由**樣本內 IC 符號**固定，不偷看 OOS。

| # | 訊號 | 公式 | 正規化 | 方向先驗 |
|---|------|------|--------|----------|
| **S1** | 外資選擇權淨部位 `foreign_opt_net` | (買權 long_oi−short_oi) − (賣權 long_oi−short_oi)，外資 on TXO | z60 | 領先，>0 偏多 |
| **S2** | 小台散戶多空比 `mtx_retail_ratio` | −1 × (法人 MTX 淨OI) ÷ (MTX 全市場未平倉) | z60 | **反指標**，極高→減碼 |
| **S3** | P/C ratio (OI) `pcr_oi` | Σ put OI ÷ Σ call OI（TXO 全履約價） | z60 / 百分位 | 反指標，極高→偏多 |
| **S4** | Max Pain 偏離 `maxpain_dev` | (close−MaxPain)/MaxPain；MaxPain=最小化賣方總 payoff 的 K | 僅到期週 | 均值回歸弱先驗 |

**S2 反推推導**：全市場多單=空單=總OI，故 散戶淨 = (總−法人多) − (總−法人空) = −(法人多−法人空)。因此 散戶多空比 = −法人淨OI/總OI，正值=散戶偏多。（注意：法人含避險腳 dealer_hedge，反推的「散戶」實含中實戶殘差，非乾淨散戶。）

---

## 3. 資料源（本地皆無，需接 FinMind）

**本地盤點（誠實）**：`data/stocks.db` 只有 `futures_institutional_daily`（**僅 TX 大台**三大法人 OI，2018-06→2026-07-08）；`panel.parquet` 無任何選擇權/MTX/大額欄位。**四訊號 100% 需外接**。

**但本帳號 FinMind 已可抓到**（本研究實測驗證，非付費靜默回空）：

| 訊號 | FinMind dataset | data_id | 負載 | 備援 endpoint |
|------|-----------------|---------|------|----------------|
| S1 | `TaiwanOptionInstitutionalInvestors` | TXO | 輕 ~1.5k 列/年 | TAIFEX 選擇權三大法人買賣權契約金額 |
| S2 分子 | `TaiwanFuturesInstitutionalInvestors` | MTX | 輕 | TAIFEX 三大法人區分契約 |
| S2 分母 | `TaiwanFuturesDaily` | MTX | 輕（取 `trading_session=position`、排除價差組合） | TAIFEX DailyMarketReportFut |
| S3/S4 | `TaiwanOptionDaily` | TXO | **重 ~12k 列/日** | TAIFEX `pcRatioExcel`（官方直接給每日 PCR_vol+PCR_OI）；data.gov.tw dataset 11322 |
| 大額交易人（延伸） | `TaiwanOptionOpenInterestLargeTraders` | — | 中 | TAIFEX `largeTraderOptQry`；data.gov.tw 11338 |

**S3/S4 已用 FinMind `TaiwanOptionDaily` 抓完整全史**（逐年查詢，非逐日：一次 range query 回整年，9 次呼叫涵蓋 2018-01→2026-07 共 2085 個交易日、position 盤別；配額安全，實測 5~17s/年）。逐年在本地算 `pcr_oi=Σput OI/Σcall OI`、`pcr_vol`、`maxpain=` 當日最大 OI 契約的最小化賣方 payoff 履約價，快取到 `pcr_maxpain.parquet`。四訊號合併後另存 `options_micro_data.parquet`（含 `fwd_open_open` 前視報酬）供重用。S1/S2 快取於 `opt_foreign_net.parquet`、`mtx_retail_ratio.parquet`。

---

## 4. 研究設計（專案標準，證偽優先）

- **前視控制**：TAIFEX 法人/大額 16:00–16:30 才公布，第 t 列訊號最早 t+1 開盤可交易；`fwd(t)=ix_close[t+1]/ix_open[t+1]−1`，成本 4bps/換手。
- **IS/OOS**：時序 70/30（切點 2024-02-15），方向只由 IS IC 決定。
- **Permutation**：對比同曝險隨機擇時 2000 次，取 null≥actual 比例為 p。
- **Deflated-Sharpe**：本研究僅市場單序列 + 少數訊號/正規化，OOS 樣本薄；任何正 Sharpe 都當**弱先驗**，比照 champion（DSR borderline 0.869 未過 0.95）誠實下修。
- **regime-conditioning**：另報「僅多頭（ix_close>MA200 且 MA200 上彎）」的 OOS，情緒反指標理論上僅多頭有效。
- **與價格動能共線檢定**：報 `corr_vs_champ`（champion 本身含 positioning/趨勢成分）；高相關=多為動能偽裝，非獨立 alpha。

---

## 5. 實跑結果（真實數字，全維度）

panel 2018-06-01..2026-07-29，n=1986，OOS 起 2024-02-15。
**B&H OOS Sharpe +0.39；champion（外資期貨 z60）OOS +1.12。**

### 5a. 訊號掃描（方向由 IS IC 符號固定）

| 訊號 | IC_IS | OOS Sharpe | OOS 僅多頭 | perm p | corr vs champ | 50/50 combo OOS | 曝險 |
|------|-------|-----------|-----------|--------|---------------|-----------------|------|
| **S1 外資選擇權淨 z60** | +0.060 | +0.81 | +1.12 | 0.076 | 0.42 | +1.21 | 0.47 |
| **S2 小台散戶多空比 z60** | −0.082 | +0.37 | +0.50 | 0.237 | 0.56 | +0.85 | 0.48 |
| **S3 PCR_OI z60** | +0.040 | **+1.22** | **+1.61** | **0.004** | 0.46 | **+1.39** | 0.48 |
| **S4 Max Pain 偏離 z60** | +0.033 | +0.42 | +1.03 | 0.165 | 0.42 | +0.92 | 0.47 |

（S3 方向 dir=+1：IS IC 為正 = 高 PCR_OI（put/call OI 偏高＝避險/恐慌）→ 後市偏強，正是**反指標**方向。合乎先驗。）

### 5b. Deflated-Sharpe + 殘差-IC（扣掉 champion 與 20 日動能）——**決定性檢定**

| 訊號 | OOS Sharpe(逐期) | **DSR** | SR0 haircut | raw IS IC | **殘差 IC** |
|------|------------------|---------|-------------|-----------|-------------|
| S1 外資選擇權淨 | 0.051 | 0.639 | 0.036 | +0.0598 | **+0.0226** |
| S2 小台散戶多空比 | 0.023 | 0.375 | 0.036 | −0.0824 | −0.0276 |
| **S3 PCR_OI** | 0.077 | **0.845** | 0.036 | +0.0404 | **−0.0082** |
| S4 Max Pain 偏離 | 0.026 | 0.402 | 0.036 | +0.0330 | +0.0004 |

DSR 試驗數 N=8（4 訊號 × ~2 正規化）；DSR>0.95 才算通過搜尋膨脹去化。**四訊號全數 DSR<0.95，無一通過。**

**原始訊號共線矩陣**（Pearson）：champion vs S1 = **0.25**、champion vs S3 = **0.36**、S1 vs S3 = 0.22。

**解讀（誠實，證偽優先）：**
- **S3 PCR_OI = 表面最強、實為動能偽裝，這是本維度最重要的發現。** 頭條數字最漂亮：OOS +1.22（勝 champion 1.12）、多頭 +1.61、**perm p=0.004 顯著**、50/50 combo +1.39。**但兩個硬檢定同時把它打回原形**：(1) **DSR=0.845 未過 0.95**——用 4 訊號×2 正規化的搜尋去化後，這個 Sharpe 在統計上與零無異;(2) **殘差 IC 從 +0.040 崩到 −0.008**——把 champion positioning 與 20 日價格動能迴歸掉之後，PCR 的方向預測力**幾乎完全消失**。permutation 只檢定「是否勝過同曝險隨機擇時」，不控制訊號與動能共線，所以 perm 顯著＋殘差 IC≈0 並不矛盾：PCR_OI 的擇時力 = 「動能 + 外資 positioning」的線性組合，不是新的獨立資訊。**verdict：動能偽裝（確認型），非真 alpha。**
- **S1 外資選擇權淨 = champion 的選擇權回聲，唯一保留一絲殘差的訊號。** IC 符號正確，OOS +0.81、多頭 +1.12、combo +1.21。**殘差 IC +0.0226（四者最高且為正）**——扣掉 champion+動能後仍剩極小的獨立成分，但 **DSR=0.639 遠未過關、perm p=0.076 borderline**。本質是「外資期貨 positioning 的選擇權版」，價值在**確認/加權**，不足以當獨立腳。
- **S2 小台散戶多空比 = 落後反指標，未過任何檢定。** IC 負（方向先驗成立），但 OOS +0.37、perm p=0.237、**殘差 IC −0.028（扣動能後反而更負，純落後）**、DSR 0.375。價值僅在**極端分位當反向前兆 + 多頭 regime 閘門**。
- **S4 Max Pain 偏離 = 到期釘價微結構，方向因子近雜訊。** perm p=0.165、**殘差 IC ≈ 0（+0.0004，扣動能後歸零）**、DSR 0.402。平日當方向訊號無效；僅到期週 (close−MaxPain) 均值回歸弱先驗，屬 L3 彩蛋。

**一句話 verdict：整個選擇權籌碼維度 = 動能/positioning 偽裝為主，無獨立 alpha 通過 DSR。** 最強的 S3 被 DSR + 殘差-IC 雙殺；唯一保有微弱殘差的 S1 是 champion 的回聲。價值在**確認外資期貨 champion**（同向加信心），不在新增因子。

---

## 6. lead_lag 定位、L0–L3 分層、與 champion 怎麼搭

| 訊號 | 領先/同步/落後 | 層 | verdict | 與 champion 的搭配 |
|------|----------------|----|---------|---------------------|
| S1 外資選擇權淨 | **領先**（大戶 positioning） | L2 籌碼核心 | 確認型（殘差 +0.023，DSR fail） | **確認/加權**：與外資期貨同向時+信心（combo +1.21）；共線，不當獨立腳 |
| S3 PCR_OI | **同步偏落後**（情緒鏡像） | L2 | **動能偽裝**（殘差 −0.008、DSR 0.845） | **不採為方向訊號**；頭條 OOS +1.22 是動能+positioning 組合，非新資訊 |
| S2 小台散戶多空比 | **落後 + 反指標** | L2→L3 | 落後（殘差 −0.028、未過檢定） | **前兆/過濾**：z 創高＝擁擠反轉前兆，對 champion 多單做**減碼閘門** |
| S4 Max Pain 偏離 | 微結構（非方向光譜） | **L3** | 雜訊（殘差 ≈0） | **到期週彩蛋**：僅結算週啟用，(close−MaxPain) 均值回歸弱先驗 |
| 大額交易人買賣權（延伸，未接） | **領先**（最純大戶） | L2 | 待補 | 同 S1，接資料驗證是否比 S1 更乾淨 |

分層邏輯：L0 regime（MA200/多頭）→ L1 價量 → **L2 籌碼核心（champion 期貨 positioning 為主腳；S1 選擇權 positioning 僅作同向確認/加權）** → L3 微結構（S4 到期週、S2 散戶擁擠前兆）。**S3 PCR_OI 雖數字最漂亮但被 DSR+殘差-IC 雙殺，不進入方向決策層，只保留作情緒儀表板讀數。** 上層有效才啟用下層。

---

## 7. 已知陷阱與規避

- **與價格動能共線（最大陷阱）**：S1 corr 0.42、S2 corr 0.56——多為動能偽裝。已對 champion 相關度做檢查；進一步應對 fwd return 迴歸時控制 MA200 斜率/過去 20 日動能，看殘差 IC 是否仍在。
- **PCR 定義混淆**：`PCR_vol`（成交量比，雜訊高）≠ `PCR_OI`（未平倉比，台灣主流、較穩）；本研究 S3 採 OI 口徑，勿混用。
- **散戶多空比反推誤差**：法人含 dealer_hedge 避險腳，反推「散戶」含中實戶殘差；MTX（小台）與 TMF（微台）是兩序列，勿混。
- **Max Pain 特有**：僅到期週有效，平日當方向因子=雜訊；效應微小且是 delta-hedge 副產物；TXO 週選（每週三結算）使「到期週」幾乎天天發生，須明確界定用哪個到期序列，避免用到未來 OI 的 look-ahead。
- **前視/資料延遲**：法人/大額 16:00 後才公布，嚴守 t+1 open 進場；FinMind 大額層若免費 token 取不到會**靜默回空**，勿把空資料當中性 0（本帳號實測 S1/S2 可抓）。
- **樣本內過擬合 / DSR / perm 的盲點（本維度實證教訓）**：**permutation 顯著 ≠ 真 alpha**。S3 PCR_OI perm p=0.004 看似鐵證,但 permutation 只打亂「擇時時點」、不控制訊號與價格動能的共線,所以動能偽裝訊號照樣過 perm。**真正把它證偽的是 (1) DSR<0.95(搜尋去化後 Sharpe≈0) 與 (2) 殘差 IC≈0(扣 champion+動能後預測力歸零)。** 四訊號 DSR 全數未過(0.375~0.845)；任何 OOS 正 Sharpe 都須先過這兩關才採信。極端分位/正規化窗是自由參數,DSR 試驗數要誠實計入。
- **regime 依賴**：所有情緒反指標（PCR、散戶多空比）幾乎只在多頭有效，空頭中散戶偏空常是對的，反指標失效——已加多頭-only OOS 欄。

---

## 8. 下一步

1. **（已完成）** S3 PCR_OI 全史 IS/OOS + DSR + 殘差-IC；S4 Max Pain 全史回測 → 結論：動能偽裝 / 雜訊。
2. **（已完成）** S1/S3 殘差-IC 共線控制（扣 champion + 20 日動能）→ 只有 S1 保留 +0.023 微弱殘差。
3. 接 `TaiwanOptionOpenInterestLargeTraders`（大額交易人前五/前十大特定法人買賣權淨），驗證是否比 S1 更乾淨的領先訊號——目前唯一可能翻案的方向（待補）。
4. S4 僅在「明確到期週 + 指定到期序列」回測 (close−MaxPain) 均值回歸，避免週選使「到期週」天天成立的界定模糊；平日方向因子已確認無效。
5. S3 可保留為**情緒儀表板讀數**（非方向訊號）：極端 PCR_OI 分位＋多頭 regime 作為 champion 多單的擁擠度旁證，但不進方向決策。

---

*本報告為量化研究記錄，非投資建議。作者非持牌投顧；所有訊號皆為研究性質，實盤前須自行完整驗證與風險評估。*
