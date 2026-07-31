# STATE OF DASHBOARD — 全專案收斂總表

_研究員: 首席量化研究組 · 日期: 2026-07-31 · Phase 1–6 收官 executive summary · 非投資建議_
_來源: `DEPLOYABLE_SYSTEM.md` · `rev_family_tradability.md`(可交易性裁決) · `critic_openissues.md` · `integrated_multigate.md` · `tej_fundamental_*.md` · `survivorship_bias.md` · `global_macro.md`(VIX gate)。所有數字為 `panel.parquet`(2018-06→2026-07-29)與 TEJ/FinMind parquet 實跑,非引用。_

---

## 0. 一句話總結

**16 維觀盤儀表板全部逐一證偽後,淨新增獨立 alpha = 0;唯一可部署核心仍是 tech-gated champion(系統 C)風控 overlay,唯一站得住的選股層是 rev_yoy_3m —— 但可交易性測試把它從「三腳成長因子」砍到「僅大型股多頭傾斜」,快腿(rev_mom/rev_surprise)在台股真實成本下全滅。專案已達邊際遞減,建議 STOP 廣度搜索。**

---

## 1. ★ 真增益總帳表(經 6 輪證偽仍站得住的成分)

| 成分 | 層 | 領先/落後 | DSR / perm | 可部署狀態 | 硬保留 |
|---|---|---|---|---|---|
| **Tech-gated champion(系統 C)**<br>外資期貨 z60>0 × close>MA200上彎 × VIX z60≤2 | L0 擇時 / 風控 overlay | **領先**(外資台指期 positioning) | OOS DSR **0.995→0.998**;champion 單獨 DSR 0.891 **fail**(肥尾) | ✅ **部署**為多/空手風控閘門(daily_tracker 近似,但用 MA150-stage 非驗證版 MA200-上彎) | 全樣本 Sharpe 僅 **+1.80**(非 OOS +2.68);OOS 窗剛好落在 champion 史上最強 2023-24-26;**2022 唯一空頭年虧損**,「空頭仍正」對 L0 不成立 |
| **VIX gate** | L0-c veto | 落後(恐慌確認型) | perm **p=0.035**,正交 champion;ΔDSR +0.003 | ✅ 部署為恐慌 veto(z60>2 不武裝多單) | OOS 僅砍 10 個多頭日,增益在量測誤差內;在地 VIXTWN(同日可用)理論更佳,**待補** |
| **rev_yoy_3m**(月營收 YoY 近3月均) | L1 選股 tilt | 領先(橫斷面) | perm p **0.003–0.007** 過;**Deflated-Sharpe FAIL**(best DSR 0.112 LARGE,誠實 24-trial penalty) | ⚠️ **僅**作**大型股多頭 long-side / overlay 傾斜**,疊在系統 C 內 | 中小型 gross edge 淨成本後**反轉為大型股**(MIDSMALL IS +0.03=OOS regime 僥倖,容量僅~$0.5M);+0.20~0.30 TAIEX 相關,非乾淨正交;需 winsorize 防會計基期怪獸 |
| ~~rev_mom(環比動能)~~ | L1 | (gross 正交) | net OOS **−1.6** | ❌ **REJECT** | 月頻翻倉→7–10×年換手→4.3–9.7%/yr 成本拖累,gross-flat 變 net 大負 |
| ~~rev_surprise(營收超預期)~~ | L1 | (gross 正交) | net OOS **−1.0** | ❌ **REJECT** | 同上,~7×換手;composite 三腳合成 gross OOS +1.17 被拖到 net **+0.14** |

**可交易性最終裁決(rev 家族三腳):只有慢腳存活。** 兩支「正交快腳」是成本吃掉的紙上訊號;三腳論在交易桌上死亡。rev_yoy_3m 過 permutation 但過不了 Deflated-Sharpe,且淨成本 + 容量後 Phase-5「edge 集中中小型」**反轉為大型股**——它是弱、容量真實但統計脆弱的選股傾斜,恰如其產品化早已假設的定位,**不是中小型 dollar-neutral 多空書**。

---

## 2. 全部證偽掉的維度(已查過,淨新增 alpha = 0)

毛利率 / ROE / EPS / PB 基本面因子(全 null,PB 台股反向)· breadth 市場廣度 · relative_strength 相對強度 · price_volume 量價 · 外資期貨 positioning 單獨(DSR fail 肥尾)· 融資維持率 near-call breadth(結構性冗餘,與 bull gate 作用域不相交)· options_micro 選擇權大額 · holder_concentration 集保籌碼 · ETF flows · global_macro Fed gate · short_daytrade 當沖 · 分點 branch-follow 跟單(stage-matched perm p=0.15–0.23,edge 是「他們買上升趨勢」)· crash thermometer(fresh-event 31–35% 低於隨機)· rev 三腳合成 composite(遜於純 rev_yoy_3m)。

---

## 3. 誠實殘餘風險 + 剩餘真缺口

**風險(以嚴重度排序):**
1. **單一多頭週期(首要)。** L0 與 L1 共用同一罩門:**2022(樣本內唯一趨勢性空頭)兩者皆虧**,而 OOS 依時間切分結構性地把 2022 排除。DSR 0.995/0.998 是「OOS 窗落在 champion 最強期」的產物,非穩態績效。
2. **tracker↔驗證版 caliber 差異。** 現行 daily_tracker 用 MA150-stage 而非驗證過的 MA200-上彎;分歧日經實測有「逆向選擇」(在 next-day 較差日放行)。逐日 agreement 88.1%。
3. **rev_yoy_3m restatement / vintage。** 用公告當下值,無 PIT 修正表;YoY 分母被追溯調整會污染排序(survivorship 已抓到 6131 基期畸變股單槍扭曲橫斷面)。緩解=winsorize 截尾,**尚未寫進 L1 規格**。
4. **空腿可交易性。** 台股賣方證交稅 30bps(硬成本)+ SBL 借券可得性 + 平盤下放空限制;中小型空腿未必借得到券。多空框架空腿落地性回測為零建模。

**剩餘真缺口(唯一最高優先實驗):** 流動性前 ~400 檔**全宇宙** rev_yoy_3m 實跑一次,帶成交值加權成本 + 含 30bps 賣方稅 + 保守 spread/衝擊,以「一次性」了結可交易性問題。—— 但本次可交易性測試已**決定性回答**其方向:中小型反轉、僅大型股淨存活,此缺口的答案大概率已知。

---

## 4. STOP / CONTINUE 建議

**判定:已達邊際遞減。STOP 廣度維度搜索。**

- 16 維全數證偽,淨新增獨立 alpha = 0;連續多輪「data 揭穿過擬合」同一型態(980T、adopted-44、branch-follow、crash-thermometer、rev 快腳)。再開新維度的期望報酬已低於執行成本。
- 可交易性測試是收官的決定性一槍:它把最後一個候選(rev 家族)從三腳砍到單腳、從中小型反轉到大型,**沒有留下任何未裁決的樂觀外推**。

**下一步唯一值得做的(二選一,皆非新研究):**
- **(A) 產品化 + 監控**存活成分:把驗證版 MA200-上彎 caliber 對齊進 daily_tracker、L1 加 winsorize 截尾、以誠實框架上 daily_tracker(champion 綠燈 + 大型股 rev_yoy_3m 多頭傾斜)。**推薦路徑。**
- **(B) 若仍要一個實驗**:前 ~400 檔全宇宙 rev_yoy_3m 帶真實 30bps 賣方稅 + 衝擊成本跑一次,把可交易性缺口從「大概率已知」變「已證」——但預期只會確認 large-cap-only 結論。

**不建議**繼續開任何新觀盤維度。
