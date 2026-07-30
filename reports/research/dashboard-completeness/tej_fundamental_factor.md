# TEJ 基本面因子(月營收YoY動能 / ROE品質 / PB價值)— 維度研究報告

分層定位: **L1 選股層(基本面橫斷面)** ｜ lead_lag: **月營收YoY = 慢速領先/確認(公告延遲~10日、但對次週報酬 IC 正);ROE/PB = 落後估值**
狀態: **implementable_now = True**(TEJ 資料已抓,本報告數字為 90 檔實跑,非 proxy)
研究腳本: `scripts/research/dashboard/tej_fundamental_study.py`(fetch: `tej_fundamental_fetch.py`)
輸出指標: `reports/research/dashboard-completeness/tej_fundamental_metrics.csv`
資料: `data/research/dashboard/tej_fundamental_{sale,fin,pb}.parquet`

---

## 1. 這維度是什麼 / 儀表板原本缺什麼

既有 16 維度全是 **價量 / 籌碼 / 總經**,完全沒有**基本面橫斷面選股**這一層。本維度補上三個經典 quant 因子,全用 **TEJ 真財報資料(非估算)**:

- **月營收 YoY 動能**(EWSALE `d0003` 營收YoY%):台股每月 10 日前公告上月營收,是**最高頻的基本面資訊**,學術上與 PEAD(盈餘後漂移)/ revenue-surprise 同族。
- **ROE 品質**(EWIFINQ `ac_r103` ROE(A)稅後):Fama-French RMW / quality-minus-junk 的代表。
- **PB 價值**(EWPRCD `pb_ratio` 交易所股價淨值比,日頻):HML 價值因子,低 PB = 便宜。

三者都對映 champion(外資期貨 positioning)所在的 **L0 市場擇時層之外**——這是「站多方時抱哪些個股」的 **L1 選股層**,理論上正交,值得補。

### 學術 / 專業依據

| 來源 | 洞見 | 本研究用法 |
|---|---|---|
| Chan, Jegadeesh & Lakonishok, *J. Finance* 1996《Momentum Strategies》 | 盈餘動能(SUE / revenue surprise)有獨立於價格動能的預測力,漂移數月。 | 月營收 YoY = 台股最高頻 revenue-surprise 代理;檢驗其是否為獨立 alpha 或價格動能偽裝。 |
| Fama-French 2015 五因子(RMW/CMA) | 高獲利(ROE)、低投資有溢酬。 | ROE 當品質因子對照。 |
| Fama-French 1992 (HML) / Lakonishok-Shleifer-Vishny 1994 | 低 PB 價值溢酬(成熟市場)。 | PB 當價值因子;台股成長性市場預期可能失效,證偽先驗。 |
| *Revisiting the momentum effect in Taiwan*, JAPWEF 2023 | 台灣**價格動能弱/易反轉**。 | 對比:基本面動能(營收)是否比價格動能穩健。 |
| 專案 MEMORY(股利還原盲點、regime 集中、DSR) | 除權息還原、OOS 單一多頭集中、多重測試懲罰。 | 用 TEJ 還原價 `close_adj`;分 regime 報;做 Deflated-Sharpe(N=6)。 |

---

## 2. 訊號定義(PIT / 方向 / 正規化)

橫斷面因子,對 90 檔流動大型股,每日排序,做多前 20% / 做空後 20%,等權,**每 5 日(週)再平衡**,t 日成訊 t+1 進場,成本 4bps/邊×週轉。**方向由 IS Rank-IC 符號固定**(證偽優先)。

- **PIT 嚴格**:月營收用 `annd`(公告日)、財報用 `a0003`(財報發布日)gate——只有公告日 ≤ t 的最新一筆才 forward-fill 到 t,杜絕前視。
- `rev_yoy` = 最新公告月營收 YoY%;`rev_yoy_3m` = 近 3 個月 YoY 均(平滑);`rev_accel` = 最新 − 前 3 月均(加速度)。
- `roe` = 最新公告 ROE(A)稅後;`pb_inv` = 1/PB(日頻,低 PB→高值→便宜)。
- `composite` = 方向對齊後 z(rev_yoy_3m)+z(roe)+z(pb_inv) 等權。

### 資料源(全 TEJ,已抓 90 檔 0 缺)

| 因子 | 表 / 欄位 | 覆蓋 |
|---|---|---|
| 月營收 YoY | `TWN/EWSALE`：`d0003`(YoY%)+ `annd_s`(公告日,PIT) | 90 檔 5,940 列,2021-01→2026-06 |
| ROE / EPS / 每股淨值 | `TWN/EWIFINQ`：`ac_r103`(ROE)、`ac_3990`(EPS)、`ac_200d`(BVPS)+ `a0003`(發布日) | 90 檔 1,889 列(季),2021Q1→2026Q1 |
| PB / 還原價 | `TWN/EWPRCD`：`pb_ratio`(日)、`close_adj`(除權息還原) | 90 檔 121,590 列,2021-01→2026-07 |

**誠實覆蓋限制**:TEJ E-SHOP 方案財報/營收僅 **2021+**,故只有 ~1 個 IS 循環 + ~1 個 OOS 循環,且 OOS(2024-12→2026-07)是**強多頭窗**(universe B&H OOS Sharpe +2.08 vs 全期 +1.62)。宇宙限 90 檔大型股(TEJ 逐檔配額)。

---

## 3. 實跑結果(90 檔,2021-01-04→2026-07-30,1,351 交易日;IS<2024-12-04≤OOS 70/30)

基準:等權 universe B&H Sharpe 全期 **+1.62** / OOS **+2.08**。

| 因子 | IC_IS | IC_OOS | Sharpe_IS | Sharpe_OOS | 多頭 | 空頭 | perm_p_OOS | corr_champion |
|---|---|---|---|---|---|---|---|---|
| **rev_yoy(月營收YoY)** | +0.029 | **+0.081** | +1.21 | **+2.55** | +1.58 | **+0.72** | **0.004** | +0.27 |
| **rev_yoy_3m(3月均)** | +0.016 | **+0.101** | +0.98 | **+2.34** | +1.57 | +0.37 | **0.002** | +0.26 |
| rev_accel(加速度) | +0.016 | +0.038 | +0.27 | +1.10 | +0.44 | +0.36 | 0.038 | +0.12 |
| roe(品質) | +0.019 | +0.030 | +0.84 | +0.33 | +0.47 | +0.46 | 0.246 | +0.20 |
| pb_inv(價值,低PB) | +0.006 | **−0.021** | −0.42 | **−0.96** | −0.76 | +0.03 | 0.723 | −0.24 |
| composite(合成) | +0.031 | +0.021 | +1.04 | +0.88 | +0.58 | +0.82 | 0.052 | +0.14 |

**Deflated-Sharpe(rev_yoy_3m,N=6 試驗數懲罰)**:OOS **DSR = 0.967**(SR_ann +2.35 vs 懲罰基準 +0.90);全期 DSR = 0.989。報酬 **skew +0.22 / kurt 3.5**(近常態,無肥尾)。Bonferroni:perm_p 0.002×6 = 0.012 仍 < 0.05。

**與 champion 搭配(champ-green z60>0 × 多頭 gate,rev_yoy)**:raw Sh_oos **+2.34** → 只在多頭 **+1.57** → champ×多頭 gate **+1.53**(僅 282 日)。**gate 反而降低**——因為此因子在空頭也正(+0.72),不需要用 champion 當閘門。

### 誠實解讀

1. **月營收 YoY 動能是本次唯一「夠格稱 alpha」的因子,而且比先前維度更硬**:
   - **IC 在 IS 與 OOS 同號為正**(rev_yoy +0.029/+0.081)——不像 RS(IC_IS 甚至負、edge 全來自尾端),這是**穩定單調的橫斷面預測力**。
   - **通過 Deflated-Sharpe(N=6)= 0.967**,近常態報酬(kurt 3.5)。對照:RS **未過** DSR、chip-macro champion headline **DSR borderline fail(肥尾)**。這是專案至今少數**過 DSR** 的橫斷面訊號。
   - **regime 穩健**:多頭 +1.58、**空頭仍 +0.72**(RS 空頭 −0.69、價格動能空頭 ≈0)。基本面動能比價格動能抗反轉——正好呼應 JAPWEF 2023「台灣價格動能弱」但沒說營收動能弱。
2. **但務必保留的三個 caveat(不吹成聖杯)**:
   - **這是眾所周知的因子**(revenue/earnings momentum,PEAD 族),**非新發現**;價值在於「以 TEJ 真資料在台股 90 檔驗證仍活著」,而非獨門。
   - **OOS 只有 1 個循環且偏多頭**(TEJ 2021+ 硬限制):OOS>IS 的漂亮數字有 adopted-44 式「集中單一多頭波段」殘留風險,雖然空頭 +0.72 已大幅緩解此疑慮。
   - **corr_champion +0.27**:非正交、但非冗餘(共同吃多頭 beta 的一部分),仍是**不同層**(champion=市場擇時 L0,營收動能=選股 L1)。
3. **價值因子(PB)在台股反向**:低 PB OOS Sharpe **−0.96**、IC_OOS −0.021——**便宜的輸**,典型成長市場「價值陷阱」。verdict = 反向。
4. **ROE 品質乾淨 null**:perm_p 0.246,兩 regime 都 ~+0.46,無顯著橫斷面選股力。
5. **composite 被 PB/ROE 拖累**:合成反而遜於純營收動能(OOS +0.88 vs +2.34)——**不要合成,單用 rev_yoy_3m**。

---

## 4. 分維度 Verdict

| 因子 | Verdict | 理由 |
|---|---|---|
| **月營收 YoY 動能(rev_yoy / rev_yoy_3m)** | **真 alpha(有保留)** | IC IS+OOS 同正、perm_p 0.002–0.004、**過 DSR 0.967**、近常態、空頭仍正;保留:已知因子、OOS 單循環偏多頭、與 champion corr +0.27 |
| 月營收加速度(rev_accel) | **弱確認** | perm_p 0.038 邊際、Sharpe 遠遜於 level YoY;不獨立採用 |
| ROE 品質 | **乾淨 null** | perm_p 0.246,無橫斷面選股力 |
| PB 價值 | **反向** | 低 PB OOS Sharpe −0.96、IC 轉負;台股成長市場價值陷阱 |
| composite 合成 | **稀釋** | 被 PB/ROE 拖累,遜於單一營收動能 |

---

## 5. lead_lag 定位 + 落層 + 與 champion 怎麼搭

- **lead_lag**:月營收公告有 ~10 日延遲(落後於營運),但對**次週個股報酬 IC 為正** → 資訊尚未被完全定價,屬**慢速領先/確認**族(PEAD 的漂移特性)。ROE/PB 為落後估值。
- **落層 = L1 基本面選股層**(儀表板全新一層):與 L0 regime(champion 擇時)、L2 籌碼、既有 L1 價量 RS 皆不同資訊源。
- **與 champion(外資期貨 positioning)的搭配**:
  1. **不同層正交疊加(推薦)**:champion 判「市場今天能不能站多方」(L0/擇時,z60>0),月營收動能判「站多方時抱哪些」(L1/選股)。corr +0.27 非全正交但屬不同層分工。
  2. **不需要 champion 當閘門**:實跑 champ×多頭 gate **降低** Sharpe(+2.34→+1.53),因營收動能在空頭仍 +0.72——它是**獨立的選股層**,不是 champion 的下游濾網(這點與 RS 不同,RS 空頭 −0.69 才需要 champion gate)。
  3. **與既有 L1 RS 搭**:兩者皆 L1 選股,但 RS=相對價格動能、營收動能=基本面;兩者相關性應偏低(RS 與自身動能高共線,營收與價格動能來源不同),可測「RS × 營收動能雙確認」增量(後續)。

---

## 6. 已知陷阱與規避

| 陷阱 | 說明 | 本研究如何規避 |
|---|---|---|
| **前視(公告延遲)** | 月營收/財報有公告延遲,用「財務資料日」會偷看未來。 | 一律用 `annd_s`/`a0003`(公告日/發布日)gate,只 ffill 公告日 ≤ t 的最新值。 |
| **股利還原盲點(專案級)** | 除權息未還原→假訊號。 | 用 TEJ `close_adj`(完全還原)算報酬,非原始 close。 |
| **價值陷阱(台股)** | 成熟市場價值溢酬在台股成長市場易反向。 | 方向由 IS IC 固定;實跑確認 PB 反向,不硬做多低 PB。 |
| **regime 集中 / OOS 單循環** | TEJ 2021+,OOS 偏多頭,易高估。 | 分 regime 報(空頭 +0.72 佐證非純多頭 artifact);標註 OOS 單循環限制。 |
| **多重測試過擬合** | 搜了 6 因子。 | Deflated-Sharpe(N=6)= 0.967 過關;Bonferroni perm_p 0.012 仍顯著。 |
| **合成稀釋** | 天真等權合成把好因子稀釋。 | 實測 composite 遜於純營收動能,建議單用。 |
| **宇宙偏誤** | 僅 90 檔大型股。 | 誠實標註;後續可擴中小型(TEJ 逐檔配額限制)。 |

---

## 7. 後續可做(非本次範圍)

- **擴宇宙**:中小型股(TEJ 逐檔配額,分批)——營收動能在中小型可能更強(資訊效率低)。
- **RS × 營收動能雙確認**:兩個 L1 選股層的聯合回測,測增量是否 > 各自單獨。
- **PEAD 事件版**:改成營收公告後 N 日的事件報酬(而非 always-on 橫斷面),更貼近文獻。
- **正式併入選股閘門**:champion risk-on 日 + 營收動能前段 + Weinstein Stage-2 三層 AND。
- **營收 surprise vs 分析師預期**(需 TEJ 預估表,現方案無)——現用 YoY 是 naive surprise 代理。

---

*本報告與所附腳本為量化研究記錄,非投資建議,不構成任何買賣或持有特定證券之推薦。歷史回測結果不保證未來績效。TEJ 財報資料 2021+,OOS 樣本薄,結論需更長樣本再驗證。*
