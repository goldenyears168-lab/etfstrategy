# 國際總經錨 (SOX / DXY / US10Y / VIX / Fed / 新台幣匯率) —— L0 regime + 開盤 gap 前兆維度研究

_研究員: 量化研究組 · 日期: 2026-07-30(STRAND C Fed/VIXTWN 補於 2026-07-31)· 對齊 chip-macro 方法論_
_腳本: `scripts/research/dashboard/global_macro_study.py`(STRAND A + B)· `scripts/research/dashboard/global_macro_fed_vixtwn_study.py`(STRAND C:Fed + VIXTWN)· 結果 CSV: `global_macro_metrics.csv`(A)· `global_macro_strandB_metrics.csv` + `global_macro_vix_gate.csv`(B)· `global_macro_fed_metrics.csv` + `global_macro_fed_gate.csv` + `global_macro_vixtwn_compare.csv`(C)· 外部真資料: `global_macro_data.parquet`(VIX/DXY/US10Y/USDTWD)· `global_macro_fed_data.parquet`(EFFR+FOMC 目標區間)· `global_macro_vixtwn_data.parquet`(TAIFEX VIXTWN,僅免費近3月)_

---

## 0. 一句話結論

國際錨都是 **外生的風險/資金背景,不是台股內生籌碼**;它們最高價值是 **L0 regime gate + 開盤前 gap 預期**,不是獨立 alpha。本輪把兩條都用 **真資料實跑完**:

- **STRAND A (SOX / 台積 ADR 隔夜, 本地)**: 開盤 gap 高相關是機械式、已 priced 不可交易;開盤後 intraday 續勢的表面 edge (OOS Sharpe 2.0) 對「TAIEX 自身動能 + 外資現貨流」正交化後完全崩解 (OOS Sharpe −0.77、perm p=0.87、DSR 0.005) —— 動能偽裝。
- **STRAND B (VIX / DXY / US10Y / USDTWD, Yahoo 真資料 2018→2026, 1986 對齊日)**: 當 **連續因子** 全都 **無訊號** —— |IC_OOS| ≤ 0.08、同曝險 permutation p 全部 **不顯著 (0.08–0.46)**,表面 OOS Sharpe 1.2–3.2 純粹是 long/flat 吃大盤漂移的假象。**唯一真實效果是 VIX 當事件式恐慌 gate**: VIX 60日 z-spike > 2 → 次日 TAIEX 均報酬 −0.099% vs 平時 +0.088%,**同曝險 permutation p=0.035 (勉強顯著的 risk-off 前兆)**;VIX 極值 (>40) 的 5 日均值回歸 (+0.89%) 方向對但 **不顯著 (p=0.88, n=38, 樣本太少)**。正交化顯示這些錨與 champion (fut_foreign_oi) **幾乎不共線 (corr ±0.03)** —— 不是冗餘,只是本身弱。
- **STRAND C (Fed 政策路徑 + 在地 VIXTWN,本輪補齊真資料)**: **Fed** 用 **NY Fed EFFR + FOMC 目標區間** 真資料 (2018-01→2026-07,2153 日) 跑完 —— 連續因子 (利率變動/水位 z) **IC 符號 IS↔OOS 全翻、perm p 全不顯著 (0.35–1.0)、bear regime 崩壞 → 乾淨 null**;但 **降息循環 regime gate 是真效果**: 目標上緣低於 6 個月前 (進行中降息) → TAIEX 未來 20 日均報酬 **+2.53% vs 平時 +1.15%**(同曝險 perm p=0.000),且 **與 MA200 多頭僅弱共線 (corr 0.12)**、在 **空頭中反而救援** (bear+cutting fwd20 **+3.48%** vs bear+not −0.12%) —— 典型「Fed put」。**但降息episode獨立樣本極少 (2019/2020/2024-25 ≈ 2-3 段,自相關灌 n) → IS-可信但無法 OOS-穩健,只能當 L0 背景燈,非獨立 alpha**。**在地 VIXTWN** TAIFEX 每月檔 **僅免費近 3 月** (更早付費 edatashop NT$3000/半年) → 全歷史 falsification **待補 (paywall)**;免費 3 月 (2026-05→07,61 日重疊,含 7 月台股 −8% 急殺) descriptive: 與 ^VIX level corr 0.69 / 變動 corr 0.55(**共動但非同一序列**)、VIXTWN 均值 37.8 vs ^VIX 17.4(在地波動約 2 倍),次日 TAIEX 預測 IC 在此危機小樣本 **未勝過 lag+1 ^VIX**(VIXTWN −0.10 vs ^VIX −0.25,但 n=61 單一 regime 不足下結論)→ **「去時差 VIXTWN 更優」假設 = 樣本不足未證,待付費全史補**。

---

## 1. 這維度是什麼 · 專業為何看它

「國際總經錨」把台股放進 **全球風險胃納與資金流** 的背景裡看:

- **SOX / 台積 ADR** —— 半導體是 TAIEX 權重核心 (台積約佔 30%)。美股夜盤先交易,對台股隔日 **開盤 gap** 有機械式時差領先 (新聞級實例: SOX −4.3% → 隔日 TAIEX gap −3.8%;台積 ADR +5% → 夜盤台指 +450 點)。
- **DXY 美元指數** —— EM 資金流的驅動子;美元升 → 新興市場/台股資金外流。
- **US10Y 美債殖利率** —— 折現率前置變數;台股電子=長天期成長股,對評價敏感,但符號隨通膨 vs 成長 regime 翻轉。
- **VIX 恐慌指數** —— 風險燈號;spike 常領先/伴隨回檔,極值 (40–50) 是均值回歸買點。
- **Fed 政策路徑** —— 最慢變的 macro backdrop;寬鬆循環=risk-on 背景。
- **新台幣匯率** —— 外資現貨買賣超的 FX 印記;台幣升值常同步外資匯入買股。

專業看它們,是為了回答「**現在該不該讓 champion (外資台指期 positioning) 武裝多單**」以及「**開盤會怎麼跳**」,而非拿來單獨擇時。

### 學術 / GitHub 依據

| 出處 | 對本維度的用途 |
|---|---|
| Valadkhani & O'Mahony (2024), *Applied Financial Economics* (RePEc wsi:afexxx:v19y2024i03) | VIX 升 **非對稱** 壓低 EM 報酬、美元變動影響 **對稱**;兩者併用預測力更好 → VIX(門檻/非對稱) + DXY(線性) 要 **分開建模**。 |
| Lucca & Moench (2015), *Journal of Finance* / NY Fed SR 512 | 1994 起約 80% 美股年度溢酬集中在 FOMC 前 24h,殖利率曲線平 + VIX 高時更強;**但 2015 後幾乎消失** → Fed 事件窗曾強效但衰減,只能當 regime 背景,不可當現行 alpha。 |
| AllianceBernstein Insights (實務) | VIX 月末落 40–50 → EM 股票未來 12M 平均 +64% → VIX 極值=均值回歸,支持 **非線性/門檻** 操作化而非連續因子。 |
| SOX→TAIEX 隔夜溢出 (BigGo / Focus Taiwan 等新聞級,**無嚴謹學術**) | 佐證機械式隔夜領先,但無 OOS 驗證 → 誠實標記為 **未經學術證實**,本研究即補此空白。 |
| GitHub: `wangzhe3224/awesome-systematic-trading`、`je-suis-tm/quant-trading`(VIX Calculator) | macro/vol overlay、VIX 因子建構範式參考;皆美股導向、**無人做過 OOS 驗證的「國際錨→TAIEX 因子」** → 白地仍需自建。 |

本專案先前 (chip-macro Stage5) 已得同結論:**人人主張 SOX/匯率連動,無人誠實 OOS 驗證**。

---

## 2. 訊號精確定義 (公式 / 正規化 / 方向)

美盤系列一律 **lag +1 台股交易日** (US 日 D → TW 交易日 D+1 開盤才可用),以 `merge_asof(direction=backward, allow_exact_matches=False)` 取「us_date < TW date t 的最後一筆」防前視;TWD/VIXTWN 為在地資料,同日可用。

| 錨 | 訊號定義 | 正規化 | a-priori 方向 |
|---|---|---|---|
| **SOX / SMH / ADR** | 美盤隔夜 close-to-close 報酬 | 除 20 日報酬 std → z20 | +1 (漲→台股漲) |
| DXY | Δ5d 變動 | z-score / 60d | −1 (美元升→台股跌) |
| US10Y | Δyield(日/週) + level 門檻 (>4.5% headwind) | z-score / 60d | −1 (符號 regime-dependent) |
| VIX | 21d MA<13 低 / >22 高 gate + spike z-score + 極值 40–50 均值回歸 | 門檻/非線性 | −1 (當 gate 非連續因子) |
| Fed | 近 3 月政策利率變動符號 (降/升/持平) + FOMC 事件窗 | 狀態 | +1 (寬鬆=risk-on) |
| 新台幣 TWD | ΔTWD(5d) | z-score | +1 (升值→台股漲) |

**TAIEX 報酬分解 (SOX/ADR 研究的核心)**:
- `gap = ix_open(t) / ix_close(t-1) − 1` —— 開盤跳空,**SOX 資訊在此已 priced**。
- `intraday = ix_close(t) / ix_open(t) − 1` —— 開盤後續勢,**唯一可能有 tradable 殘差 edge 的區**。
- `c2c = ix_close(t) / ix_close(t-1) − 1` —— 含 gap,相關看似高但機械。

門檻 (20d/60d/200d、Δ5d、>4.5%) **a-priori 凍結,不做網格搜尋** (避免重蹈 980T / adopted-44 過擬合前例)。

### 資料源 (誠實區分本地 vs 需接)

| 錨 | 資料狀態 (本輪) | 來源 |
|---|---|---|
| **SOX / SMH / TSM_ADR** | ✅ **本地齊備** `data/stocks.db` `daily_bars` source='yahoo', 2019-01-02→2026-07-29 | `src/yahoo_chart_sync.py` 已生產同步 |
| **DXY** | ✅ **已抓真資料** 2018-01→2026-07 (2157 列) | Yahoo v8 chart `DX-Y.NYB` (真 ICE DXY) |
| **US10Y** | ✅ **已抓真資料** 2018-01→2026-07 (2154 列;`^TNX` 已是 yield% 本身,如 4.62) | Yahoo v8 chart `^TNX` |
| **VIX** | ✅ **已抓真資料** 2018-01→2026-07 (2156 列) | Yahoo v8 chart `^VIX` |
| **USDTWD** | ✅ **已抓真資料** 2018-01→2026-07 (2233 列) | Yahoo v8 chart `USDTWD=X`(國際報價,非台銀牌告) |
| **Fed** | ✅ **已抓真資料** 2018-01→2026-07 (2153 日,目標上緣 1.50%→3.75%) | **NY Fed markets API EFFR**(FRED CSV/API 本環境 timeout,改用權威原始源;含 FOMC 目標區間 targetRateFrom/To,可精準抽政策事件)→ `global_macro_fed_data.parquet` |
| 在地 VIXTWN | ⚠️ **部分** 僅免費近 3 月 (2026-05→07,62 日) | TAIFEX 每月檔 `.../Dailydownload/vix/log2data/YYYYMMnew.txt`(big5;僅最近 ~3 月免費,全史付費 edatashop NT$3000/半年)→ `global_macro_vixtwn_data.parquet`;**全史 falsification 待補** |

四錨真資料存於 `data/research/dashboard/global_macro_data.parquet`(`fetch_external_to_parquet()` 直打 Yahoo v8 chart API 產出,yfinance 常被 rate-limit 故改直打)。`run_strand_b()` 讀此 parquet → merge_asof(+1 TW 日防前視) → 同一 `evaluate_anchor` 框架。**Fed / VIXTWN 需 FRED key / TAIFEX 頁面,本輪未接,誠實標待補。**

---

## 3. 研究設計 (依專案方法論,證偽優先)

1. **時差防前視 (最致命)**: 美盤系列 lag +1 TW 日 (見 §2)。FRED 日頻會回溯修訂 → 真 point-in-time 應用 ALFRED；回測用 Yahoo 同美盤日 close 較安全。
2. **方向 a-priori / 由 IS IC 符號定**,不偷看 OOS。
3. **IS/OOS 70/30 時間分割**。
4. **同曝險 permutation**: 固定相同做多天數、隨機挑日,2000 次,算 OOS Sharpe 分位 p 值。
5. **Deflated-Sharpe** (Bailey & López de Prado): PSR 對抗 `expected_max_SR`,含 skew/kurtosis 調整,門檻 0.95;六錨 × 多 transform 搜尋空間大 → DSR 幾乎必 fail,是誠實下修關卡。
6. **regime-conditioning**: 分 bull(>MA200)/bear 報 OOS Sharpe (預期 edge 只在多頭出現)。
7. **共線控制 (本維度核心)**: 把 intraday 續勢對 **TAIEX 自身 20 日動能 + 外資現貨淨買超 (foreign)** 做 OLS 正交化,量「beyond 動能 & 資金流」的淨增量 —— 這正是揭穿 margin 因子=動能代理的同一手法。

---

## 4. 實跑結果 (STRAND A — SOX / SMH / ADR,本地 1835 個對齊交易日)

| 訊號 | IC_IS | IC_OOS | OOS Sharpe | perm p | OOS DSR | OOS bull | OOS bear |
|---|---|---|---|---|---|---|---|
| SOX_z20 → **gap[priced]** | +0.567 | +0.607 | **+6.30** | 0.000 | 1.000 | +6.61 | +5.82 |
| SOX_z20 → intraday[residual] | +0.182 | +0.268 | +2.04 | 0.000 | 0.947 | +2.40 | **−0.17** |
| SOX_z20 → c2c[mechanical] | +0.399 | +0.485 | +4.83 | 0.000 | 1.000 | +5.31 | +3.08 |
| SMH_z20 → intraday[residual] | +0.179 | +0.270 | +2.40 | 0.000 | 0.986 | +2.82 | +0.05 |
| TSM_ADR_z20 → intraday[residual] | +0.119 | +0.235 | +1.76 | 0.000 | 0.885 | +2.15 | −0.31 |
| **SOX_z20 ⟂(mom20+foreign) → intraday** | +0.045 | **−0.085** | **−0.77** | **0.867** | **0.005** | −0.59 | −2.04 |
| **TSM_ADR_z20 ⟂(mom20+foreign) → intraday** | −0.021 | −0.119 | −1.35 | 0.994 | 0.000 | −1.46 | −0.68 |

### 解讀 (三層剝洋蔥,每層砍掉一塊假 edge)

1. **gap[priced] 的 Sharpe 6.3 是假象**: 相關高 (IC 0.6) 但那是機械式 —— 台股 **開盤那一刻 SOX 資訊已完全反映在跳空**。除非能在開盤前/開盤瞬間成交,否則不可交易。c2c[mechanical] 的 4.8 同理 (它含 gap)。
2. **intraday 殘差看似有 edge (Sharpe 2.0) 且非「gap-and-fade」**: 開盤後仍有續勢 (IC_OOS 0.27),但 **bear regime 失效** (SOX −0.17 / ADR −0.31) 且 **DSR 0.885–0.986 未穩過 0.95** → 已是 regime-dependent 的弱訊號。
3. **決定性證偽**: 對 TAIEX 自身 20 日動能 + 外資現貨流 **正交化後,edge 完全崩解** —— IC 掉到 +0.045(IS)/ −0.085(OOS)、**OOS Sharpe −0.77、perm p=0.87 (隨機都比它好)、DSR 0.005**。**SOX 隔夜的表面 intraday 續勢,幾乎全是「台股自身動能 + 外資資金流」的偽裝,沒有獨立增量資訊。**

**結論**: SOX / ADR = **開盤 gap 的確認性前兆 (機械、已 priced)**,不是可持有的獨立 alpha。與 memory 7/28「夜盤假彈」警告一致。

> 重跑: `python scripts/research/dashboard/global_macro_study.py`

---

## 4B. 實跑結果 (STRAND B — VIX / DXY / US10Y / USDTWD,Yahoo 真資料 1986 對齊交易日 2018-06→2026-07)

真資料抓自 Yahoo v8 chart (`^VIX` / `DX-Y.NYB` / `^TNX` / `USDTWD=X`),存 `data/research/dashboard/global_macro_data.parquet`;美盤系列一律 lag +1 TW 交易日 (`merge_asof backward, allow_exact=False`) 防前視。

### (a) 當「連續因子」→ TAIEX 前瞻報酬 (Δ5d z60,VIX 用 level z60;方向 a-priori)

| 錨[target] | IC_IS | IC_OOS | OOS Sharpe | perm p | OOS DSR | bull | bear |
|---|---|---|---|---|---|---|---|
| DXY[fwd1] | +0.003 | −0.008 | +1.33 | 0.14 | 0.54 | +1.64 | +0.19 |
| DXY[fwd5] | +0.039 | +0.075 | +2.37 | 0.46 | 0.96 | +2.32 | +2.68 |
| US10Y[fwd1] | −0.025 | −0.021 | +1.22 | 0.22 | 0.48 | +1.81 | −1.63 |
| US10Y[fwd5] | −0.005 | −0.066 | +3.02 | 0.08 | 1.00 | +3.23 | +1.65 |
| VIX[fwd1] | −0.061 | +0.002 | +1.22 | 0.28 | 0.48 | +1.04 | +3.00 |
| VIX[fwd5] | −0.072 | +0.006 | +3.23 | 0.08 | 1.00 | +3.16 | +3.86 |
| USDTWD[fwd1] | +0.008 | +0.007 | +1.38 | 0.13 | 0.58 | +1.46 | +1.19 |
| USDTWD[fwd5] | +0.032 | −0.041 | +2.75 | 0.14 | 0.99 | +2.81 | +2.39 |

**解讀 — 全都無訊號**: 每一列 |IC| ≤ 0.08(近零相關),而唯一誠實的判準 —— **同曝險 permutation p —— 全部不顯著 (0.08–0.46,無一 < 0.05)**。看似漂亮的 OOS Sharpe 1.2–3.2 與高 DSR(fwd5 尤甚)是 **假象**: long/flat 曝險 ~50%、fwd5 又是重疊視窗(自相關灌 Sharpe),抓的是 **TAIEX 本身多頭漂移**,不是因子區辨力。permutation 直接證明「隨機挑同樣多天」跟它一樣好。

### (b) VIX 當 L0 恐慌 gate (事件式,同曝險 permutation 5000 次) —— 這才是真效果

| gate | target | n | gate 內均報酬 | gate 外均報酬 | perm p(null≤obs) |
|---|---|---|---|---|---|
| VIX 60d z > +1.5 (spike) | 次日 c2c | 242 | **−0.034%** | +0.088% | 0.069 (邊際) |
| **VIX 60d z > +2.0 (大 spike)** | 次日 c2c | 157 | **−0.099%** | +0.088% | **0.035 (顯著)** |
| VIX > 30 (絕對高) | 5 日 | 146 | +0.524% | +0.367% | 0.74 (不顯著) |
| VIX > 40 (極值→均值回歸) | 5 日 | 38 | +0.887% | +0.369% | 0.88 (不顯著, n 太少) |

**解讀**: VIX 的價值不在連續因子而在 **事件 gate**。**大 spike (60日 z>2) 顯著標記次日 risk-off** (−0.10% vs +0.09%, perm p=0.035),量級小但方向確定,適合當「別在恐慌噴出當天追多」的 veto 燈。VIX 極值 (>40) 的 5 日 **均值回歸反彈** (+0.89%) 方向符合 a-priori (恐慌極值=逆勢買點),**但 permutation 不顯著 (p=0.88, n=38 樣本太少)** —— 只能當弱先驗,不敢當訊號。

### (c) 正交化 ⟂ (foreign 現貨流 + champion fut_foreign_oi Δ5d) + 對 champion 共線

| 錨 ⟂ | IC_IS | IC_OOS | OOS Sharpe | perm p | corr(champ) | corr(foreign) |
|---|---|---|---|---|---|---|
| DXY | +0.012 | +0.009 | +0.80 | 0.51 | −0.025 | −0.173 |
| US10Y | −0.024 | −0.016 | +1.16 | 0.25 | +0.030 | −0.072 |
| VIX | −0.053 | +0.026 | +0.89 | 0.46 | +0.027 | −0.285 |
| USDTWD | +0.017 | +0.025 | +0.37 | 0.78 | −0.011 | −0.180 |

**解讀**: 兩個發現。① 正交化後 IC 仍近零、perm p 仍不顯著 —— 這些連續因子 **本來就不是訊號**,正交化沒抹掉什麼(不同於 STRAND A 的 SOX,SOX 是被正交化「揭穿」為動能偽裝;這四錨是 **一開始就沒 edge**)。② 與 champion 的相關 **微乎其微 (±0.03)**,與 foreign 現貨流也只 −0.07~−0.29 —— 代表 **不冗餘**: 特別是 **USDTWD 與 foreign corr 僅 −0.18**,先前擔心「TWD = 外資現貨流 FX 印記會重複計數」在日頻 Δ5d 尺度上 **證據不足**;但因 TWD 本身無 edge,不冗餘也不代表可用。

### VERDICT (STRAND B)

| 錨 | verdict | 落層 | 一句話 |
|---|---|---|---|
| **VIX** | **前兆 (弱但真)** | **L0 gate** | 大 spike(z>2)顯著標記次日 risk-off(p=0.035);極值均值回歸方向對但不顯著。當恐慌 veto 燈,非連續因子。 |
| **DXY** | **待補/無訊號** | L0 背景 | 連續因子 IC≈0、perm 不顯著;不共線 champion。當背景燈,無獨立 alpha。 |
| **US10Y** | **待補/無訊號** | L0 背景 | 同 DXY;bear regime 甚至翻負(符號不穩),只能條件化背景。 |
| **USDTWD** | **待補/無訊號** | L0/同步 | 連續因子無訊號;意外地與 foreign **不冗餘**(corr −0.18),但本身無 edge。 |
| **Fed** | **背景/降息put(真但episode少)** | **L0 背景** | 連續因子 null;**降息 gate** 顯著抬升 fwd20 且救援空頭,但獨立 episode 極少→非 OOS-穩健,當背景放行燈。 |
| 在地 VIXTWN | **待補 (paywall)** | L0 | 免費近 3 月不足;與 ^VIX corr 0.69 共動,未證更優。 |

**整維度定位**: **不是獨立 alpha**。VIX 是唯一站得住的真效果 —— 一個 **弱但顯著的 L0 恐慌前兆/gate**(用來 veto champion 的多頭武裝,非並列第二支腳);DXY/US10Y/USDTWD 當連續因子 **無訊號**,只能當最上層 risk 背景燈。與 champion 的搭配是 **條件化/否決**,不是正交 alpha 疊加 —— 且因與 champion 幾乎不共線,VIX veto 是 **非冗餘** 的補充。

> 重跑: `python scripts/research/dashboard/global_macro_study.py --with-external`(讀本地 parquet;`--refetch` 重抓 Yahoo)

---

## 4C. 實跑結果 (STRAND C — Fed 政策路徑 + 在地 VIXTWN,gap-fill 補齊,2026-07-31)

補既有兩個「待補」錨。腳本 `scripts/research/dashboard/global_macro_fed_vixtwn_study.py`(重用 STRAND A/B 同一 `evaluate_anchor`)。

### (a) Fed 政策路徑 → TAIEX (連續因子;NY Fed EFFR + FOMC 目標區間,2153 日;US 序列 lag+1 TW 日)

| 訊號[target] | IC_IS | IC_OOS | OOS Sharpe | perm p | OOS DSR | bull | bear |
|---|---|---|---|---|---|---|---|
| Fed_easing3m[fwd5] | +0.102 | **−0.074** | +0.76 | 1.00 | 0.31 | +2.08 | −4.59 |
| Fed_easing3m[fwd20] | +0.180 | **−0.157** | +1.87 | 1.00 | 0.87 | +4.13 | −8.26 |
| Fed_easing6m[fwd20] | +0.082 | **−0.140** | +5.65 | 0.75 | 1.00 | +6.49 | +2.60 |
| Fed_level_z[fwd5] | −0.103 | **+0.037** | +3.68 | — | 1.00 | +4.64 | +0.67 |
| Fed_level_z[fwd20] | −0.236 | **+0.089** | +7.93 | 0.52 | 1.00 | +9.57 | +2.60 |
| Fed_easing6m ⟂(foreign+champ)[fwd20] | +0.086 | −0.125 | +7.43 | 0.35 | 1.00 | +8.58 | +2.60 | corr_champ −0.03 / corr_foreign −0.06 |

**解讀 — 連續因子=乾淨 null**: 每一列 **IC 符號 IS↔OOS 全翻**(easing IS 正→OOS 負;level IS 負→OOS 正),這是無訊號的教科書指紋;唯一誠實判準 **同曝險 perm p 全部 0.35–1.0(無一顯著)**,漂亮的 OOS Sharpe 5–8 純是 OOS 多頭窗 long/flat 吃漂移(fwd20 重疊視窗灌 Sharpe,同 STRAND B),bear regime 一律崩壞。正交化後仍 null,與 champion 幾乎不共線(−0.03)→ **不冗餘但本身無 edge**。

### (b) Fed 降息 regime GATE(事件式,同曝險 permutation 5000)—— 這才是真效果

| gate | target | n | gate 內均報酬 | gate 外均報酬 | perm p |
|---|---|---|---|---|---|
| **降息循環 (目標上緣<6M前)** | fwd20 | 643 | **+2.53%** | +1.15% | **0.000 (高於隨機)** |
| 升息循環 (目標上緣>6M前) | fwd20 | 600 | +0.71% | +1.99% | 0.000 (低於隨機) |
| 降息循環 (目標上緣<6M前) | fwd5 | 643 | +0.66% | +0.24% | 0.001 (高於隨機) |
| FOMC 降息事件當日→ | fwd5 | 11 | — | — | n<20 (樣本太少) |

**bull/bear 交叉(揭穿是否只是多頭 regime 重述)**:

| | cutting (降息中) | not-cutting |
|---|---|---|
| **bull (>MA200)** | +2.29% (n=516) | +1.74% (n=901) |
| **bear (<MA200)** | **+3.48% (n=127)** | **−0.12% (n=422)** |

**解讀 — 真但 episode 少**: 降息循環顯著抬升 TAIEX fwd20(+2.53% vs +1.15%,perm p=0.000),**且與 MA200 多頭僅弱共線(corr 0.12,P(bull|cutting)=0.80 vs 0.69)** → 不是把既有多頭 gate 換句話說。最有力的是 **空頭中的降息反而把 fwd20 從 −0.12% 翻成 +3.48%** —— 典型「**Fed put**」(寬鬆救市)。**但致命限制: 2018–2026 只有 ≈2–3 段獨立降息 episode(2019 mid-cycle / 2020 COVID / 2024–25),643 個「降息日」高度自相關,有效獨立 n 極低**(與 margin C3 深斷頭 n=4 同一陷阱)→ **IS-可信,無法 OOS-穩健,不敢當交易訊號,只能當 L0 背景放行燈**。與 a-priori(寬鬆=risk-on 背景)一致。

### (c) 在地 VIXTWN(同日,去美盤時差) vs ^VIX(lag+1)—— 免費 3 月 descriptive

TAIFEX VIXTWN **全歷史付費(edatashop NT$3000/半年)**,免費只有最近 3 個月。本輪抓到 **2026-05-04→07-30(62 日;與 panel 重疊 61 日)**,恰含 7 月下旬台股 −8% 急殺(TAIEX 07-27→07-29 43634→40039)。

| 指標 | 值 | 說明 |
|---|---|---|
| corr(VIXTWN, ^VIX) level | **0.69** | 共動但非同一序列 |
| corr(ΔVIXTWN, Δ^VIX) | 0.55 | 日變動中度連動 |
| VIXTWN 均值 / ^VIX 均值 | **37.8 / 17.4** | 在地波動約 2 倍(TAIEX 更集中、電子權重高 + 本窗危機) |
| 次日 TAIEX 預測 IC(高波→低報酬,dir −1) | VIXTWN **−0.10** vs ^VIX(lag+1) **−0.25** | 危機窗兩者皆呈「高波→次日反彈」(均值回歸);此小樣本 VIXTWN **未勝過** lag+1 ^VIX |

**解讀 — 假設樣本不足未證**: 任務核心問「去時差的 VIXTWN 是否優於 lag+1 ^VIX」。在唯一可得的免費 61 日(且是單一危機 regime)中,VIXTWN 與 ^VIX 高度共動(0.69),但 **次日預測力並未勝過** lag+1 ^VIX。n=61 單 regime 遠不足以做 IS/OOS+permutation → **判定 verdict='待補'(需付費全史)**;先驗上「在地無時差應更即時」仍合理,但 **本輪無法證實**。

> 重跑: `.venv/bin/python scripts/research/dashboard/global_macro_fed_vixtwn_study.py`(`--refetch` 重抓 NY Fed + TAIFEX)

---

## 5. lead/lag 定位 · 落層 · 與 champion 怎麼「搭配」

對照 champion = 外資台指期 positioning (領先, chip×bull OOS +1.79):

| 錨 | 實測 verdict | 落層 | 與 champion 的關係 |
|---|---|---|---|
| **SOX / 台積 ADR** | 動能偽裝 (gap 已 priced) | **L0 / 前兆** | **確認**: 開盤前提供 gap 預期;夜盤大跌 → 當日別在弱開盤追多。不加 alpha,只調當日進場時機。 |
| **VIX** | **前兆 (弱但真, p=0.035)** | **L0 gate** | **否決**: 大 spike(z>2)當日 veto champion 多頭武裝(次日 risk-off 顯著);與 champion 幾乎不共線 → 非冗餘補充。 |
| **DXY** | 待補/無訊號 (IC≈0) | **L0 背景** | **過濾 (弱)**: 連續因子無區辨力,只能當美元強弱背景燈。 |
| **US10Y** | 待補/無訊號 (符號不穩) | **L0 背景** | **過濾 (條件化)**: bear regime 翻負,不可與 champion 線性疊加。 |
| **USDTWD** | 待補/無訊號 | **L0/同步** | **意外非冗餘 (corr foreign −0.18)** 但本身無 edge → 不採用。 |
| **Fed 政策路徑** | **背景/Fed put(真但 episode 少)** | **L0 最上層** | **放行/救援**: 連續因子 null;**降息循環** 顯著抬 fwd20(+2.53%)且救空頭(bear+cut +3.48%),但 ≈2–3 段獨立 episode 無法 OOS-穩健 → 背景燈,非交易腳。與 champion 不共線(−0.03)。 |
| 在地 VIXTWN | **待補 (paywall)** | **L0 gate** | 免費近 3 月不足;與 ^VIX corr 0.69,未證更即時。付費全史補後可望取代 ^VIX 當 **去時差恐慌 gate**。 |

**總定位**: 真資料證實 —— **沒有一個錨是獨立 alpha,也沒有一個比 champion 更領先**。站得住的增量價值 = **VIX 大 spike 當 L0 恐慌 veto**(弱但顯著,非冗餘)+ **Fed 降息循環當 L0 背景/Fed put 放行燈**(真效果但獨立 episode 少,不敢當訊號);DXY/US10Y/USDTWD/Fed 連續因子 **全無訊號**,只能當最上層風險背景燈。角色是 **否決/過濾/放行 champion**,不是與之並列的第二支獨立腳。

---

## 6. 已知陷阱與規避

1. **時差/前視 (最致命)**: 美盤 D 日資料台股 D 日盤中不存在,最早 D+1 開盤可用;FRED 有發布延遲。→ 已用 `merge_asof(backward, allow_exact_matches=False)`;FRED 建議 ALFRED point-in-time,回測優先 Yahoo 同日 close。
2. **與價格動能共線 (本研究已實證命中)**: SOX≈全球科技 beta=TAIEX 動能偽裝;TWD=外資流 FX 印記。→ **一律先做正交化 (⟂ mom20+foreign) 量淨增量**,原始相關不採信。
3. **開盤 gap 已 priced**: SOX 領先是機械時差,gap 那刻已反映;→ 只當 gap 前兆,不當持有 alpha。
4. **符號不穩 / regime 反轉**: US10Y-股票相關會翻 (2022 股債齊跌);VIX 線性幾乎無效只有極值/regime。→ 全部 **跨 regime 條件化**,不當固定線性因子。
5. **日曆效應衰減**: Pre-FOMC drift 2015 後消失。→ IS 漂亮 ≠ 現行有效,Fed 只當背景。
6. **多重測試**: 6 錨 × 多 transform × 多時間窗 → DSR 幾乎必 fail (champion DSR 全程未過 0.95)。→ 凍結門檻、一次性 OOS、n_trials 校正的 DSR。
7. **資料口徑 (本輪已用 Yahoo)**: DXY=`DX-Y.NYB`(真 ICE DXY,非 FRED broad DTWEXBGS);US10Y=`^TNX`(已是 yield% 本身,4.62 即 4.62%,非舊制 ×10);USDTWD=`USDTWD=X`(國際報價,非台銀牌告買賣價);VIX=`^VIX`(有美盤 +1 日時差 → 在地 VIXTWN 更佳,待補)。→ 報告明列;Yahoo 同日 close 無回溯修訂,較 FRED 適回測。
8. **美台假日錯位**: 固定 shift(1) 會在長假前後錯位。→ 用 `merge_asof` 依實際日期對齊而非固定位移。

---

## 7. 實作優先序 (誠實建議)

1. **立即可用 (零工程)**: SOX/ADR gap 前兆已在 `tech_risk_daily_snapshot` 生產化 —— 當「開盤前 gap 預期 + 弱開盤別追多」的 **確認燈**,不當獨立訊號。
2. **唯一實測站得住的**: **VIX 大 spike (60日 z>2) 當 L0 恐慌 veto** —— 次日 risk-off 顯著 (p=0.035) 且與 champion 幾乎不共線 (非冗餘)。可接進 L0 gate,恐慌噴出當天壓低 champion 的多頭曝險;**量級小,定位 veto 燈非 alpha**。下一步優先 **抓在地 VIXTWN**(去美盤時差,同日可用,應優於 ^VIX)驗證同一效果。
3. **無訊號,只當背景**: **DXY / US10Y / USDTWD / Fed 連續因子** IC≈0(Fed 更是 IS↔OOS 符號全翻)、permutation 不顯著,**不新增任何交易腳**;僅在 dashboard 當風險背景燈。
4. **Fed 降息循環當 L0 背景/Fed put 放行燈**(gap-fill 已補真資料): 降息中 fwd20 顯著抬升且救空頭,與 champion 不共線 —— 可在 dashboard 當「寬鬆背景放行、緊縮背景收斂 champion 多頭曝險」的最上層燈;**但獨立 episode 僅 2–3 段,務必標『非 OOS-穩健、非交易訊號』**。Fed 資料源: **NY Fed EFFR API**(FRED 本環境 timeout)。
5. **在地 VIXTWN 待補 (paywall)**: TAIFEX 全史付費(NT$3000/半年),免費僅近 3 月不足做 falsification;免費窗顯示與 ^VIX corr 0.69 共動、未證更優。**建議**: 若要正式驗「去時差 VIXTWN > lag+1 ^VIX」,需採購 edatashop 全史(2007+)或改用 MacroMicro/investing.com 長序列。目前 L0 恐慌 gate 仍用 ^VIX(STRAND B,p=0.035)。
6. **USDTWD 冗餘疑慮已釐清**: 與 foreign 現貨流 corr 僅 −0.18,**非高度共線**(先前假設過重);但因本身無 edge,結論仍是不採用。

---

_非投資建議。本報告為研究證偽用途,所有結果基於歷史資料回測,不保證未來表現,不構成任何買賣建議。_
