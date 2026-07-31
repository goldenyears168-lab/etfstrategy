# TEJ 還原股價 攻破「股利調整/存活偏誤」盲點 — 維度研究報告

分層定位: **L1 價量層(資料品質稽核 + RS 重驗)** ｜ lead_lag: **N/A(這是資料正確性稽核,非訊號)**,承載的 RS 訊號同步偏落後(momentum-class)
狀態: **implementable_now = True**(TEJ EWPRCD 已抓,本報告數字為實跑結果)
研究腳本: `scripts/research/dashboard/tej_cleanprice_fetch.py`(抓取)、`scripts/research/dashboard/tej_cleanprice_blindspot_study.py`(稽核+RS重跑)
輸出: `reports/research/dashboard-completeness/tej_cleanprice_q1_metrics.csv`、`tej_cleanprice_rs_metrics.csv`、`data/research/dashboard/tej_cleanprice_data.parquet`

**verdict = 確認(confirmation)+ 盲點局部乾淨(clean-null on the feared magnitude)**:除權息調整量體確實巨大(5年累積 median +27%,金融股 +34%),用**未還原價**跑 RS 會嚴重失真 —— 但 Phase-1 RS 研究實際用的 finmind `adj_close` **已是真還原**,與 TEJ 專業還原價逐日報酬 median 相關 0.991、日報酬 MAD 僅 2.5bps。把 RS 四訊號整組搬到 TEJ 乾淨還原價重跑,**結論零改變**:Mansfield RSM 仍為唯一去動能偽裝、正交 champion、強 regime 依賴的最佳形式,乾淨價還把 OOS Sharpe 從 +1.81 微升到 +2.08。原先擔心的「RS 跑在未還原資料上」盲點,在流動性宇宙上**並未成真**。

---

## 1. 這維度是什麼 / 為何要攻它

專案 MEMORY 反覆記到兩個「專案級盲點」:**incomplete dividend adjustment** 與 **survivorship bias**。RS 報告第 5、6 節也自陳:`stock_daily_bars.adj_close` 全表僅約 10% 有值,除權息日未還原價會把 RS 扭成假訊號。本維度用 **TEJ EWPRCD 專業級全還原價(還原股利+分割,`close_adj`)** 當作 ground truth,做兩件事:

- **Q1 稽核**:量化除權息調整的量體(尤其高殖利率金融股),並檢驗 Phase-1 實際用的 finmind `adj_close` 到底是「真還原」還是「未還原/半還原」。
- **Q2 重驗**:把 RS 四訊號原封不動搬到 TEJ 乾淨還原價重跑同一套 falsification gauntlet(IS/OOS + permutation + regime + 去 champion 共線),看 Phase-1 的 RS 結論會不會被推翻。

### 資料源與覆蓋(誠實)

| 項目 | 來源 | 覆蓋 |
|---|---|---|
| 專業還原價(ground truth) | TEJ `TWN/EWPRCD`:`close_adj`(全還原)、`close_d`(原始)、`cdiv_ratio` | 90 檔流動宇宙,**2021-01-04 → 2026-07-30**,121,590 筆。TEJ E-SHOP 方案資料**僅 2021+**(硬限制)。 |
| 被稽核對象 | `data/stocks.db → stock_daily_bars`(finmind `close`/`adj_close`) | 同 90 檔;RS 報告即用此 `adj_close`。 |
| 大盤 | `data/stocks.db → daily_bars code='IX0001'` | RS 分母,兩組共用以隔離「個股價還原」單一變因。 |
| champion | `chip_macro/panel.parquet` `fut_foreign_oi` z60 | 跨層去相關。 |

**宇宙 = RS 研究的同一批 90 檔流動股**(含 16 檔金融:2882/2881/2891/2884/2886/2883/2887/2892/2890/2880/2801/5871/5880/5876/2885/2834),使數字可與 Phase-1 直接對照。

---

## 2. Q1 — 除權息調整盲點量化(實跑)

### 2a. 調整量體:確實巨大,未還原=嚴重低估總報酬

`adjfac = close_adj / close_d`,`cum_div_adj = adjfac(2026)/adjfac(2021) − 1` = 這 5.5 年若用未還原價會**低估的累積漲幅**:

| 群組 | 累積還原調整(2021→2026) |
|---|---|
| 全 90 檔 median | **+27.4%** ｜ mean +39.1% ｜ max +434.8%(3293,含大額股票股利/減資) |
| 金融股 median | **+34.2%** |
| 非金融 median | +23.0% |

範例(國泰金 2882):`adjfac` 由 0.757(2021)升到 1.000(2026),每年 6 月底除息一次階梯下修,5 年累積調整 ~24%。**這證實:用未還原價跑 RS,高殖利率金融股在每年除息日會被打出一根假跌、且長期相對強度被系統性低估** —— 盲點的「危害若成真」的量體是真的大。

### 2b. 但 finmind `adj_close` 其實已是真還原(盲點未成真)

關鍵稽核:finmind `adj_close` 的**逐日報酬**到底貼近 TEJ 的還原價還是原始價?

| 指標(90 檔 median) | 值 | 解讀 |
|---|---|---|
| corr(finmind_adj 報酬, **TEJ_adj** 報酬) | **0.9906** | 貼近專業還原價 |
| corr(finmind_adj 報酬, **TEJ_raw** 報酬) | 0.9832 | 明顯**不是**未還原(否則此值應更高、且上一列應更低) |
| MAD(finmind_adj − TEJ_adj) 日報酬 | **2.52 bps**(max 10.2) | 逐日平均差極小 |
| corr(finmind_raw, TEJ_raw)(sanity) | 0.9980 | 原始價互相對得上,管線正確 |

**→ Phase-1 RS 研究實際跑在「真還原」價上,不是未還原。** 之前報告自陳的「僅 finmind + 流動性過濾才有 ~99% adj」的防呆是有效的;feared blind spot 在流動宇宙上**沒有發生**。

### 2c. 殘餘瑕疵(次階、誠實記錄)

finmind 還原並非完美。少數檔 finmind_adj 與 TEJ_adj 逐日報酬 MAD 偏高、相關偏低,多為金融/近期大量配息名:

| stock | 金融 | 累積調整% | corr(fin_adj,tej_adj) | MAD bps |
|---|---|---|---|---|
| 5871(中租) | Y | +49.0 | 0.971 | **10.2** |
| 6488(環球晶) | N | +17.2 | 0.927 | 8.0 |
| 2834(臺企銀) | Y | +36.6 | **0.876** | 7.4 |
| 5347(世界) | N | +29.8 | 0.961 | 6.5 |
| 2801(彰銀) | Y | +29.8 | 0.940 | 5.6 |

這些是 finmind 還原因子的近似誤差(除息基準日/現增權值處理差異),量級 5–10bps/日,屬**二階**、不改 RS 結論,但若未來要做**單檔精算**(如個股期望殖利率、除息缺口交易)應改用 TEJ 還原價。

### 2d. 附帶發現:finmind 原始 `close` 欄維護不全

重跑時 finmind **原始價** RS 直接算不出 Sharpe(NaN):其 `close` 欄的 last-valid 日期參差(07-17 → 07-29 散落),近端資料未同步。這本身就是「**別用原始價**」的證據 —— finmind 把維護重心放在 `adj_close`,原始 `close` 是二等公民。

---

## 3. Q2 — RS 在 TEJ 乾淨還原價上重跑(2021+,實跑)

同一套橫截面五分位多空 + IS/OOS 70/30 + permutation(同曝險 1000 次)+ regime + 去 champion/去自身動能共線。**三組價格源、同 2021+ 窗**對照:

### TEJ `close_adj`(乾淨還原,ground truth),90 檔,B&H Sharpe(全) = +1.62

| 訊號 | IC_IS | IC_OOS | Sharpe_IS | Sharpe_OOS | 多頭 | 空頭 | corr_自身動能 | corr_champion | OOS_perm_p |
|---|---|---|---|---|---|---|---|---|---|
| **mansfield_rsm50** | −0.008 | +0.057 | +0.368 | **+2.085** | +1.790 | −0.293 | **+0.386** | −0.077 | **0.000** |
| ibd_rs_rank | +0.027 | +0.045 | +1.026 | +1.261 | +1.652 | −0.384 | +0.917 | +0.064 | — |
| rs_mom_252_21 | +0.038 | +0.031 | +1.160 | +0.734 | +1.270 | +0.193 | +1.000 | +0.063 | — |
| gh_52w_high | +0.018 | +0.026 | +0.264 | +0.673 | +1.197 | −0.706 | +0.123 | −0.203 | — |

### finmind `adj_close`(同窗對照)B&H = +1.39

| 訊號 | Sharpe_OOS | 多頭 | 空頭 | corr_動能 | corr_champ | OOS_perm_p |
|---|---|---|---|---|---|---|
| **mansfield_rsm50** | **+1.814** | +1.585 | −0.358 | +0.386 | −0.078 | 0.007 |
| ibd_rs_rank | +1.348 | +1.724 | −0.316 | +0.916 | +0.062 | — |
| rs_mom_252_21 | +1.126 | +1.490 | +0.246 | +1.000 | +0.062 | — |
| gh_52w_high | +0.218 | +0.966 | −0.670 | +0.218 | −0.213 | — |

### finmind 原始 `close`:Sharpe 算不出(NaN,見 2d)—— 原始價不可用。

### 誠實解讀

1. **結論零改變。** 乾淨還原價下,四訊號**排序、性質完全一致**:Mansfield RSM 仍是唯一(a)與自身動能低共線(+0.39,vs IBD +0.92、rs_mom +1.00 幾乎就是動能本身)、(b)正交 champion(−0.08)、(c)強 regime 依賴(多頭 +1.79 / 空頭 −0.29)的最佳形式。IBD/rs_mom 仍是動能偽裝,gh_52w_high 仍弱。
2. **乾淨價微幅『增強』而非推翻。** mansfield OOS Sharpe 由 finmind +1.81 → TEJ +2.08,permutation p 由 0.007 → **0.000**。方向對:去掉金融股除息日的殘餘假跌雜訊,讓真還原價的橫截面稍乾淨一點。這是「盲點修正**強化**既有發現」,不是「盲點修正**推翻**發現」的那種戲劇性反轉。
3. **原有 caveat 全數保留。** IC 仍近零(mansfield IC_IS −0.008),Sharpe 仍幾乎全來自尾端分位 + 多頭 regime;2021+ 窗更短、更集中多頭,OOS>IS 的漂亮數字「集中單一多頭」嫌疑比 Phase-1 更重。**乾淨價沒有把 RS 變成 standalone alpha** —— 它仍是多頭 regime 下的 L1 個股強弱濾網。
4. **未過 DSR 的判斷不變**:乾淨價只修了資料品質,沒減少搜尋自由度(仍 4 訊號 × 參數),不宜宣稱獨立 alpha。

---

## 4. 存活偏誤(survivorship)—— 誠實:本次只修了一半

本 rerun 用的 90 檔是「**存活到 2018–2026 仍流動**」的名單,**本身就是存活者**。TEJ 修正的是**除權息還原**,不是存活偏誤 —— RS 在 TEJ 上的漂亮數字仍是 conditioned on survivors。要真正攻存活偏誤,需 TEJ **含下市股**的歷史成分(EWPRCD 對已下市 coid 仍有資料,但需先取「當時的流動宇宙快照」名單,是更大一次抓取)。**verdict on survivorship = 待補**(非本次配額範圍內完成;已知方向:納入下市名會**降低**動能/RS 的 backtest 績效,與台股反轉體質一致)。

---

## 5. 落層 + lead_lag + 與 champion 搭配

- **落層 = L1 價量(資料品質底座)**:本維度不是新訊號,是**校準 L1 RS 所依賴的價格資料正確性**。結論:流動宇宙的 finmind `adj_close` 品質足以支撐 L1 RS;單檔精算需升級 TEJ。
- **lead_lag = N/A**(稽核);其承載的 Mansfield RSM 同步偏落後(momentum-class),窄用途前兆同 RS 報告。
- **與 champion 搭配不變**:corr(mansfield, champion) 乾淨價 −0.077 ≈ 正交。兩層分工照舊 —— champion(L0/外資期貨 positioning)決定市場能否站多方,RSM(L1)決定站多方時抱哪些個股;champion risk-off 時 RSM 空頭 −0.29 印證退場。

---

## 6. 陷阱與規避

| 陷阱 | 本研究處置 |
|---|---|
| 誤以為 finmind adj_close 未還原 → 全盤否定 Phase-1 | 實測 corr 0.991 / MAD 2.5bps,**證實已還原**,Phase-1 站得住 |
| 用原始價跑 RS | 實測金融股 5年累積調整 +34%,除息假跌;且 finmind 原始 close 維護不全(NaN)—— 雙重理由禁用原始價 |
| TEJ 只 2021+ 卻拿去比 2018 起的 Phase-1 | 明確**同窗對照**(TEJ_adj vs finmind_adj 皆 2021+),隔離價格源單一變因,不跨窗誤比 |
| 把「乾淨價微升 Sharpe」吹成新 alpha | 明列 IC 近零、regime 集中、未過 DSR,結論仍為多頭濾網非 alpha |
| 存活偏誤假裝也修好了 | 誠實標 survivorship = **待補**,宇宙仍是存活者,需下市名快照另抓 |
| 單檔精算誤用 finmind 近似還原 | 標註 5871/2834/2801/6488 等 5–10bps 殘差,精算改用 TEJ |

---

## 7. 後續(非本次)

- **存活偏誤真攻**:抓 TEJ 各年度「當時流動宇宙」含下市 coid,重跑 RS,量化 backtest 縮水(預期 RS/動能績效下修)。
- 單檔除息缺口/期望殖利率交易:以 TEJ `close_adj` + `cdiv_ratio` 建精算,勿用 finmind 近似。
- 用 TEJ 全還原價回填 `stock_daily_bars` 殘差偏高的金融名(5871/2834/2801),供 live RS 用。

---

*本報告與所附腳本為量化研究記錄,非投資建議,不構成任何買賣或持有特定證券之推薦。歷史回測結果不保證未來績效。TEJ 資料涵蓋 2021+,存活偏誤僅局部修正。*
