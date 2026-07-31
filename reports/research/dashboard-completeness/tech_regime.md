# 技術趨勢 / 均線 / Weinstein 階段 / regime —— L0 regime gate 維度研究

_研究員: 量化研究組 · 日期: 2026-07-30 · 對齊 chip-macro 方法論_
_腳本: `scripts/research/dashboard/tech_regime_study.py` · 結果 CSV: `reports/research/dashboard-completeness/tech_regime_gates.csv`_

---

## 1. 這維度是什麼 · 專業為何看它

「趨勢 / 均線 / Weinstein 階段 / regime」在本專案分層架構中是 **L0 層(regime gate)**,不是獨立 alpha 源。它把「市場現在處於什麼狀態」操作化成一個開火閘門:**只決定已驗證的領先籌碼訊號(外資台指期 positioning, `fut_foreign_oi_z60`,champion)何時允許武裝多單**,自身不預測進場時點。

專業把「Weinstein 階段」拆成三個各有同儕文獻、彼此高度重疊的零件來回測(而非驗證「四階段標籤」本身):

- **Weinstein 階段 + Mansfield RS**(實務標準):週線 30 週 MA(≈150 日)+ MA 斜率兩軸定四階段;僅 **Stage2(價 > 上彎 30wMA)** 允許做多,Stage4 一律 flat/veto。Mansfield RS = 個股/大盤比值 − 其 52 週 MA,零軸穿越判相對強弱。
- **時間序列動能 TSMOM**(Moskowitz-Ooi-Pedersen 2012, JFE):用過去 12 個月自身報酬的「符號」定方向,等價於一種乾淨正規化的慢速趨勢濾網。
- **MA 擇時 + 波動縮放**(Faber 2007;Moreira-Muir 2017, JF):200 日 MA 月頻擇時主要貢獻是**砍尾部回撤**而非加報酬;高波動時反向降曝險可提高趨勢/動能因子 Sharpe。

### 學術 / GitHub 依據
| 出處 | 對本層的用途 |
|---|---|
| Moskowitz, Ooi & Pedersen (2012, JFE), SSRN 2089463 | 12M 報酬符號趨勢濾網,58 市場穩健;當 L0 TSMOM 同號確認 |
| Faber (2007, SSRN 962461) | 10 個月(≈200 日)SMA gate + 月頻再平衡壓 whipsaw;主砍回撤 |
| Moreira & Muir (2017, JF), NBER w22208 | 實現波動反向縮放部位 → 提升動能/趨勢 Sharpe(邊際升級,非細分階段) |
| Weinstein (1988) + Mansfield RS | 30wMA + 斜率四階段;實務共識僅 Stage2 有效 |
| Llorente-Michaely-Saar-Wang (2002, RFS);Gervais-Kaniel-Mingelgrin (2001, JF) | 「量只在知情時確認趨勢」→ 解釋原始量能濾網反傷 edge,故 L1 併入籌碼 L2 |
| GitHub: `ksanjay/stage-analysis-stocks`、`benwaldner/pine-scripts`(weinstein.pine/mansfield.pine)、`shiyu2011/cookstock`(Stage2/VCP) | 階段/RS 分類邏輯移植參考;皆美股 yfinance、無台股籌碼、從不 OOS 驗證 |

本專案先前校準(LAYERED_DESIGN + eval_stage7)已證:**Weinstein 四階段「分類」實測並未勝過純 close>MA200,Stage2 ≈ MA200**。故本研究把 L0 操作化為「MA200 + 斜率」粗粒度 regime,不細分四階段。

---

## 2. 訊號精確定義(公式 / 正規化 / 方向)

訊號一律用 `close(t)` 計算,act on `t+1`(`fwd` = 次日 close→close 報酬),long-only。

| 訊號 | 定義 | 方向 |
|---|---|---|
| `close_gt_ma200` | `ix_close > SMA200(ix_close)` | 多(a-priori) |
| `stage2_proxy`(≈Weinstein Stage2) | `close>MA200` **且** `MA200.diff(22)>0`(200 日 MA 近月上彎) | 多 |
| `ma200_slope_up` | `MA200.diff(22)>0` | 多 |
| `tsmom_12m` | `ix_close.pct_change(252) > 0`(12M 報酬符號) | 多 |
| `tsmom_6m` | `pct_change(126) > 0` | 多 |
| `volscaled_bh`(Moreira-Muir) | 連續部位 = `clip(median(realvol)/realvol, ≤1)`,`realvol`=20 日報酬 std | 多、連續 |
| **champion**(對照) | `fut_foreign_oi_z60 > 0`,z60 = 相對自身 60 日均值標準化 | 多 |

門檻(150/200 日、12M、22 日斜率)**a-priori 凍結,不做網格搜尋**(避免重蹈 980T / adopted-44 過擬合前例)。

### 資料源
- **指數層 L0 完全本地可跑,零外接**:`data/research/chip_macro/panel.parquet`(`ix_open/high/low/close` = TAIEX,2018-06→2026-07-29,1986 列)。champion 原始值 `fut_foreign_oi` 亦在 panel。
- **個股 RS 延伸(exploratory)**:`data/stocks.db` `stock_daily_bars.adj_close`(2504 檔還原價)÷ `daily_bars` code=`IX0001`(2015→今)算 Mansfield RS。
- 若要**正式**做個股階段/RS/突破回測,需補全**含已下市股票**的還原價宇宙:FinMind `TaiwanStockPriceAdj` + 下市集合(現行 FinMind 主要在市標的)——這是實作前必補的資料完整性缺口,非新資料源。

---

## 3. 研究設計(依專案方法論,證偽優先)

- **IS/OOS 時間分割**:70/30(IS < 2024-02-15,OOS 596 日)。方向只由 IS IC 符號決定,不偷看 OOS。
- **Permutation**:vs **同曝險隨機**——固定相同做多天數、隨機挑日,2000 次,算 OOS Sharpe 分位 p 值。
- **Deflated-Sharpe**(Bailey & López de Prado):PSR 對抗 `expected_max_SR`(7 個 gate trials + champion,共 8),含 skew/kurtosis 調整,門檻 0.95。
- **Regime-conditioning**:分 bull(>MA200)/ bear(≤MA200)分別報 champion Sharpe。
- **共線性檢定**:算各 gate 與 60 日價格動能相關,揭露「這些訊號本質就是動能」。

---

## 4. 實跑結果(本地 panel,已實際執行)

Buy&Hold: IS Sharpe **+0.66** / OOS **+1.41**。成本 4bps/邊。

### (1) 標準 regime gate 單獨—— 無穩健獨立 edge
| signal | exposure | OOS Sharpe | perm p |
|---|---|---|---|
| `stage2_proxy` | 0.65 | **+1.60** | 0.133 |
| `close_gt_ma200` | 0.72 | +1.56 | 0.182 |
| `volscaled_bh` | 0.85 | +1.46 | — |
| `tsmom_12m` | 0.68 | +1.22 | 0.774 |
| `ma200_slope_up` | 0.69 | +1.16 | — |

最佳 gate(stage2_proxy)OOS +1.60 僅微幅高於 B&H +1.41,**同曝險 permutation p=0.13(不顯著)**;tsmom_12m p=0.77(等同隨機)。→ **標準趨勢 gate 單獨沒有勝過 B&H 的統計證據**。這正是預期:它們是動能的定義,不是獨立資訊。

### (2) Regime-conditioning——champion edge 幾乎全在多頭
| regime | champion OOS Sharpe | 天數 |
|---|---|---|
| ALL | +1.81 | 596 |
| **bull (>MA200)** | **+2.78** | 526 |
| bear (≤MA200) | +0.26 | 70 |

champion 在空頭幾乎失效(+0.26)。→ 證實方法論第 3 條:edge 只在多頭出現。

### (3) 用 regime gate 過濾 champion——L0 的真正價值
| gate | champ OOS | **gated OOS** | champ maxDD | **gated maxDD** | exposure |
|---|---|---|---|---|---|
| `close_gt_ma200` | +1.81 | **+2.61** | −18.5% | **−3.9%** | 0.26 |
| `stage2_proxy` | +1.81 | +2.61 | −18.5% | −3.9% | 0.23 |
| `tsmom_12m` | +1.81 | +1.87 | −18.5% | −11.1% | 0.24 |
| `ma200_slope_up` | +1.81 | +1.63 | −18.5% | −18.5% | 0.25 |

以 `close>MA200` 閘門過濾 champion:OOS Sharpe **+1.81→+2.61**、最大回撤 **−18.5%→−3.9%**,permutation **p=0.000**。→ **L0 的價值是「砍掉空頭尾部」而非加報酬**(呼應 Faber),與 champion 是「確認/過濾」關係。注意:MA200 斜率當閘門反而略傷(+1.63),斜率濾網在此窗過度延遲。

### (4) 共線性——這些訊號就是價格動能(最致命陷阱)
`corr(close_gt_ma200, 60d 動能)=+0.63`、`tsmom_12m=+0.43`、`ma200_slope_up=+0.49`。→ 任何在 Stage2 上條件化的籌碼/分點訊號,**必須做 stage-matched permutation** 才能分辨真 skill 與「只是買在上升趨勢」(本專案 branch-follow 已踩坑:富邦新店→景碩 stage-matched p=0.15–0.23)。

### (5) Deflated-Sharpe(8 trials)
| 策略 | ann Sharpe | PSR(vs0) | DSR | 判定 |
|---|---|---|---|---|
| best gate = stage2_proxy | +1.60 | 0.991 | 0.832 | **FAILS** 0.95 |
| champion alone | +1.82 | 0.995 | 0.891 | **FAILS** 0.95 |
| **gated champion @>MA200** | +2.61 | 1.000 | **0.995** | **SURVIVES** 0.95 |

單獨 gate 與 champion 單獨皆過不了 DSR;**唯有「gated champion」通過**。但須誠實下修(見第 5 節)。

---

## 5. lead/lag 定位 · 落層 · 與 champion 搭配

- **lead/lag**:作為**訊號**落在 **同步→落後**格(MA 黃金/死亡交叉在底/頭後數週才確認;TSMOM 用 12M 過去報酬本質滯後;Mansfield RS 零軸穿越略領先於純價-MA 但仍是對已發生轉折的確認)。**切勿當前兆**(前兆格屬分點賣超/融資新增廣度那類事件訊號)。
- **落層**:**L0(regime gate)**。上層 L0 決定下層(L2 籌碼核心 = champion)何時有效。
- **與 champion(領先,外資期貨 positioning)搭配 = 過濾 / 確認**:
  - champion 是領先訊號但**只在多頭有效**(bull +2.78 / bear +0.26);
  - L0 的職責是:**Stage4 / 價 ≤ MA200 時,任何夜盤 / 盤中 / 籌碼確認都不得重新武裝多單**;
  - 實測此過濾把 champion OOS Sharpe +1.81→+2.61、maxDD −18.5%→−3.9%,且 DSR 由 0.891(fail)升到 0.995(survive)。**這是本維度對整個系統的唯一、但真實的貢獻。**

---

## 6. 已知陷阱與規避

1. **與價格動能共線(最致命)**:gate 與 60d 動能 corr 0.4–0.6,它是動能的定義。→ 下游任何在 Stage2 上條件化的籌碼訊號一律用 **stage-matched permutation**(同曝險 + 同階段抽樣)。
2. **Whipsaw / 區間盤失效**:MA 交叉在 Stage1/3 頻繁假訊號。→ 加斜率濾網、緩衝帶、月頻再平衡;僅 Stage2 開火。
3. **前視偏誤**:訊號 close(t) 計算、act on t+1;週線 MA 勿用未收完當週;Mansfield 52 週 MA 用 trailing 非 centered。本腳本 `fwd = close.pct_change().shift(-1)` 已隔離。
4. **存活者偏誤 + 未還原價(全專案盲點)**:個股 Stage2/RS 突破必須用**還原價 + 含已下市宇宙**,否則報酬灌水(基金研究約高估 0.9%/年)。本報告個股 RS 僅 5 檔 demo、明標 exploratory、**不下 OOS 結論**。
5. **Regime 依賴 / 單週期過擬合(本研究最大保留)**:OOS 窗(2024-02→2026-07)596 日中僅 **70 日空頭**,幾乎全多頭。「gated champion 通過 DSR」是在**單一多頭週期**取得,**尚未跨一個完整多空循環**;DSR 的 `var_trials` 用保守 1/n 近似。→ 定性為**強力候選、非定論**,須再過一個空頭週期方可信。
6. **樣本內過擬合**:門檻 a-priori 凍結,不網格搜尋(980T / adopted-44 前例)。
7. **資料延遲**:FinMind 還原價除權息回填時點、盤後才定的週 K,勿用當下不可得資訊。

---

## 7. implementable_now 判定

- **本地即可實作(已實跑)**:指數層 L0 regime gate 全套(MA200/斜率/TSMOM/波動縮放 + regime-conditioning + gated-champion + permutation + DSR)——`panel.parquet` 直接跑,零外接。**implementable_now = true。**
- **需補資料完整性(非新 source)**:個股層 Stage2/RS/突破的正式 OOS 驗證,需 FinMind `TaiwanStockPriceAdj` + 下市宇宙補存活者偏誤;本報告僅 demo 管線可跑。

---

_本報告為量化研究之證偽性分析,非投資建議。所有訊號僅供研究討論,不構成任何買賣、持有之推薦。過去回測表現不代表未來績效。_
