---
name: branch-specialist-funnel
description: >-
  Stock-first branch-specialist funnel: P4 four-gate → P5 pool → P6 efficiency
  grid → P7 merge into mini 20:00 expert-pool watch. Also covers optional seed-
  branch discovery. Use for 分點專家/expert pool/國巨/南亞科/群聯/watch adoption.
---

# 分點專家漏斗（股票優先 · 可自動上 mini）

Research only · 未採納不寫 `config/strategy.yaml` · 對齊 `docs/terminology.md`、
`reports/research/branch-footprint-screen/分點專家卡.md`。

## 一句話

**預設入口 = 股票名單 S**，不是種子分點。  
監測對象 = hard 四門通過的 **(B,S) 專家池** + P6 冠軍規則。  
種子分點 B0 僅作 **可選發現模式**（探針 ≠ 終點）。

**已檢驗登錄（防重複）**：`reports/research/branch-footprint-screen/expert_pool/EVAL_REGISTRY.json`  
（人類可讀：`EVAL_REGISTRY.md`）。開跑前先查 registry；P4–P7 結束必須 upsert（含 adopted／retired／skipped_hard0）。

## 規模化：目標採納 ~80 檔

不要用「高 beta」當**選股**主篩（選股看煙霧密度）。大盤放大用 **固定 β=1.15** 在超額裡校正（見協議）。

**2026-07-21 實證**：12h 擴到 U0dense/U0broad 後停在 **~43–44/80**；`skipped_hard0`≈55%；**br8=1 → hard0≈100%**。  
詳見 `expert_pool/SCALE80_SCREEN_REDESIGN.md`。達 80 需 **新的 br8≥3 tape**，不是再掃 U0 海。

### Step 0 — 預篩（P4 之前 · 必跑）

```bash
PYTHONPATH=src python3 scripts/research/screen_expert_pool_prefilter.py --lane both
# → expert_pool/SCALE80_PREFILTER_QUEUE.json
```

| 鍵 | 定義 |
|----|------|
| **br8** | 該股上達 episode **n≥8** 的本土分點數（= P4 可進場候選數） |
| **cons2 / cons3** | 同日 ≥2 / ≥3 家本土分點各淨買 ≥0.5億 的日數 |

| 階 | 篩法 | 用途 |
|----|------|------|
| **BAN** | **br8≤1** | **永不進隊列**（禁 U0dense / U0broad） |
| **PRIMARY** | br8≥3 · ep≥30 · (cons3≥3 **或** cons2≥8) | 預設檢驗序（覆蓋既有採納 ~91%） |
| **SOFT** | br8≥2 · ep≥18 · cons3≥5 | 僅 PRIMARY 空後；覆蓋薄池 hard=1 |
| 參考 U2 | ep≥60 · br8≥4 · 週波動≥6% | PRIMARY 內優先序，非另開 U0 |
| 降權 | mega（如 2330）· 高 br8 低起伏 | 往後排 |

**禁止**：為湊 80 而把 ep 門檻往下開到 br8≤1。  
通過率預算改抓 **PRIMARY ~25–35%**（β=1.15 後）；SOFT 更低。PRIMARY 未檢名單不足時 **報告天花板並停**，勿燒 12h 海掃。  
**adopted 數到 80 停**（或硬停檢驗預算 / 預篩隊列空）。

漏斗進程：**序列**（`mode=ro`）；IX0001 報酬可一次快取共用。固定 1.15 **幾乎零額外成本**。

## 凍結協議（每次開跑先貼出）

| 鍵 | 預設 |
|----|------|
| window | 近 2 年（例 `2024-07-01` → 資料末日） |
| event | 淨買金額 ≥ **0.5億**（股數×收） |
| episode | 同股去重 **5** 曆日 |
| return | **L1H7**：T+1 open → H7 close · 成本 **30bps**（只扣個股） |
| bench | **IX0001**（`daily_bars` · source 優先 yahoo→tej→finmind） |
| **excess** | \(r_{\text{adj}} = r_{\text{股}} - 1.15 \times r_{\text{IX0001}}\)（**固定 β=1.15** · 不計複利） |
| OOS cut | **`YYYY-01-01`** 最近完整年（例 2026-01-01） |
| P4 min n | 該 B 在 S 上 episode **≥8** |
| P4 同股差 | 中位相對同股候選宇宙 **>+1pp**（hard） |
| P4 專長差 | S 中位 − ¬S 中位 **>+1pp**；¬S n<8 →「單核」：需同股差+跨期 |
| P4 跨期 | ≥2 個半年窗中位不全 ≤0 |
| **P7 超額門檻** | 冠軍規則下各筆 \(r_{\text{adj}}\) 之 **中位數 ≥ +2%** |
| **主資料** | `stock_broker_branch_daily` · **source=`finmind` only** |
| **價量** | 個股：`stock_daily_bars` finmind；基準：`daily_bars` IX0001 |

改任何鍵 = 新實驗，必須重貼協議。  
**不做**：個股滾動 β、改用 0050 當主超額基準（0050 可另做診斷對照，不進 P7 門檻）。

## 預設主路徑（使用者給股票名單）

```text
查 EVAL_REGISTRY（已檢則跳過）
    → Step0 預篩（BAN br8≤1 · PRIMARY → SOFT）
    → P4 四門（本土分點 · hard）
    → P5 寫死池（2–5；hard=1 標「單薄」）
    → P6 完整網格（單次）
    → 算冠軍訊號 L1H7 之 r_adj 中位（β=1.15×IX0001）
    → 過門？寫 watch_spec + upsert registry → 批次結束後一次併 POOLS / sync mini
```

### 自動升級規則（P7 / mini）

| 條件 | 動作 |
|------|------|
| hard≥2 **且** \(r_{\text{adj}}\) 中位 ≥ +2% | **自動**登錄 watch（批次結束再 sync mini） |
| hard=1 **且** \(r_{\text{adj}}\) 中位 ≥ +2% | 同上，池標 **「單薄」** |
| hard=0 **或** \(r_{\text{adj}}\) 中位 < +2% | **不上排程**；registry 標 retired／skipped；不擋同批其他 S |

已淘汰例（2026-07-20 · 當時為裸超額 vs IX0001）：`3017` `4979` `6770` `8299`。  
大規模開跑前可用 β=1.15 **重算既有 8 檔**是否仍過門（協議變更＝新實驗）。

### P5 選池

- 只收 **hard** 通過分點，按同股差 desc，取 **2–5** 家。  
- 控制組可選，不進寄信核心。  
- 禁止敘事：「某某分點擅長 S」若未 hard 過。

### P6 完整網格（每次必跑 · 不拆多 agent）

| 維度 | 取值 |
|------|------|
| 模式 | OR · 同日共識≥2 · **回看1日共識** |
| 門檻 | ≥0.5億 · ≥1億 |
| 政策 | H7/H10 × restart / **skip** / **extend** |
| 報告 | 可列複利／效率作參考；**P7 決策只看 \(r_{\text{adj}}\) 中位** |

冠軍優先序：回看1日共識 > 同日共識 ≫ OR；skip/extend ≫ restart。  
腳本：`scripts/research/run_stock_expert_funnel.py --stock {S}`。

### P7 排程（唯一 live 入口）

- 腳本：`scripts/research/run_expert_pool_watch.py --stock all`  
- launchd：`com.jackm4.goldenstocks.winbond-expert-pool-watch`（**僅 mini** · 週一至五 **20:00**）  
- 新 S：併入同一 POOLS（`watch_spec.json` 自動合併）；不要另開 launchd。  
- **大批次**：先全部檢驗完 → 一次 rsync／bootstrap，勿每檔都重裝。  
- 觸發信／日報正文含 **進場註記**（訊號收、建議限價＝收×1.01、追價≤3% 開盤上限、SMA5 與軟例外開盤上限）。  
- 觸發信另含 **研究成績單**（A/B/C/D 分級 · hard · 中位 r_adj · 冠軍規則 · 一句話 · **備註**）＋近期 L1H7 交易摘要；完整逐筆在知識庫。  
- **交易知識庫**：`expert_pool/knowledge/`（`TIER_NOTES.md` · `INDEX.md` · `trades/{sid}.md|.json`：訊號日、分點今/昨、真實／大盤／超額%、進出場）。重建：`scripts/research/build_expert_pool_trade_knowledge.py`。  
- 圖文 digest（手動／CX）：`scripts/research/send_expert_pool_chart_digest.py`（K線＋成績單＋全歷史交易表）。  
- **僅主訊號達標才寄信**（40+ 池不發「今日無訊號」洗版；`EXPERT_POOL_QUIET_EMAIL=1` 可恢復）。  
- **賣方觀測**：同日≥2 core HARD（`W0_hard`）僅在**綠燈主信**內附註；無綠燈不單獨寄。持倉通道見 20:10 `holdings_branch_sell_monitor`（主徽章 `W0_HARD`）。出場 SSOT 仍 L1H7（見 `hypotheses/H_EXPERT_SELL_OBS_DESIGN.md`）。  
- 回放模擬：`run_expert_pool_watch.py --simulate-dates YYYY-MM-DD,...` → `expert_pool/email_sim/`。

### 觀測進場建議（人工下單註記 · 非自動送單）

研究樣本：既有 8 sleeve · ~96 訊號 · 追價>3%≈33（L1H7 · β=1.15 · 30bps）。  
郵件／日報**繼續全寄**，並附計算後的訊號收／建議限價／SMA5 軟例外價；下列供人判斷，**不**當硬過濾掉信、**不**進 Order launchd。

**凍結規則（首選）**

| 情境 | 動作 |
|------|------|
| 追價（T+1 開 ÷ 訊號收 −1）**≤3%** | 開盤／市價可做 |
| 追價 **>3%** | 限價掛 **訊號收 × 1.01**（對齊 tick） |
| 當日未成 | **同價**續掛，合計約 **≤3 個交易日** |
| 仍未成 | **放棄本次訊號**（不要隔日開市價追） |
| 軟例外 | T+1 開已在 **SMA5 ≤＋8%**（昨收算均、PIT）→ 可開盤做，不必死等限價 |

**不要當主規則**

- 必須觸碰短均才買（SMA5／10／20 觸價）：棄單多或中位虛高。  
- SMA3／WMA3 當「不要差太多」門檻：太貼價，易變相死追；若要用 3 日線最多 **WMA3 ≤＋8%**，仍不如 SMA5。  
- 限價沒成 → 隔日開市價：高追子集隔日開多半仍貴（中位常 +8～10%）。  
- 限價放寬到訊號收＋2% 以上：中位接近死追。

**對照（高追子集中位 \(r_{\text{adj}}\)，約數）**：死追開 ~+3.8% · 限收＋1%→隔日開 ~+5.2% · 限＋1%≤3日棄 ~+7.2% · 開若 SMA5≤＋8% 否則限 ~+8.0%。  
細節：`expert_pool/entry_filter_study_8sleeves.md`；腳本 `scripts/research/run_expert_sleeve_entry_filters.py`。

## 可選：發現模式（種子分點）

僅當沒有股票名單：`P0→P1→P2→P3` 登錄 ≤3 檔 S → 接主路徑。

## 並行上限

| 項 | 上限 |
|----|------|
| 同輪研究 agent | **≤5**（彙整用） |
| 同輪漏斗進程 | **序列** `run_stock_expert_funnel.py` |
| 同輪種子 B | 發現模式：1 主 + 可選 1 對照 |

## 資料平面

| 表 | 角色 | 本漏斗 |
|----|------|--------|
| `stock_broker_branch_daily` | 分點逐席 | **主** |
| `stock_daily_bars` | 個股日K finmind | **主** |
| `daily_bars` | IX0001 | **主（基準）** |
| 其他 | — | 非主 |

## 本庫錨點

| 用途 | 路徑 |
|------|------|
| 單股漏斗 P4–P6 | `scripts/research/run_stock_expert_funnel.py` |
| 規模預篩 br8/cons | `scripts/research/screen_expert_pool_prefilter.py` |
| 已檢驗登錄 | `expert_pool/EVAL_REGISTRY.json` · `.md` |
| 規模 redesign | `expert_pool/SCALE80_SCREEN_REDESIGN.md` |
| P7 觀測 | `scripts/research/run_expert_pool_watch.py` |
| 進場過濾研究 | `scripts/research/run_expert_sleeve_entry_filters.py` · `expert_pool/entry_filter_study_8sleeves.md` |
| launchd | `com.jackm4.goldenstocks.winbond-expert-pool-watch`（mini · 20:00） |
| 報告根 | `reports/research/branch-footprint-screen/expert_pool/{S}/` |

## 禁止

- 種子總榜 = 專家；未完成 P5／超額門檻就 P7  
- Book 裝 live launchd；雙機雙跑 Order  
- hard=0 或 \(r_{\text{adj}}\) 中位&lt;2% 仍寫進 POOLS  
- **br8≤1 / U0dense / U0broad 海掃湊採納數**  
- 並行多進程搶寫 SQLite schema  
- 為完整度硬跑 P0–P2（已有名單時）  
- 把進場限價／均線規則寫成自動送單（僅註記；Order 另議）

## 新名單／大批次檢查清單

- [ ] 貼凍結協議（含 **β=1.15 · 中位≥2%**）  
- [ ] 讀 `EVAL_REGISTRY` · 跳過已檢  
- [ ] **Step0** `screen_expert_pool_prefilter.py` · **禁 br8≤1** · PRIMARY→SOFT  
- [ ] 預篩未檢 PRIMARY 不足時：報告天花板，**不要**開 U0 海  
- [ ] 序列跑 funnel · 每檔 upsert registry  
- [ ] 批次結束：合併 POOLS → 一次 rsync mini → bootstrap  
- [ ] 彙整表：adopted／retired／skipped 計數  
