# TMF 微型臺指 · Channel Final v1.2+ · 技術白皮書 / PRD

| 欄位 | 內容 |
|------|------|
| 版本 | **final_v1_3_0_pv16_celltune_v2**（`recipe_version` SSOT：`src/order/tmf_channel_pv16_book.py`） |
| 日期 | 2026-08-06（celltune v2 採納＋架構切開：引擎進 `src/tmf_channel` · 16:03 cutover 上線） |
| 狀態 | **Order 層**；recipe v1.3.0：session×PV8＝16 cell（SPECIALIZED＋CELL_TUNE_V2 疊加）；四鎖 + **唯一執行路徑＝launchd KeepAlive `tmf-channel-poll` worker**（重用 Fubon session；禁止 nohup 雙跑；見 §7.2） |
| 商品 | 微型臺指期貨 **近月**（Fubon `tickers` 自動解析） |
| 引擎 | **`src/tmf_channel/causal_engine.py`**（`tmf_channel.engine` · hang_anchor=O）；lab `hang_anchor_causal_lab.py` 僅 shim |
| Paper UI | `reports/research/channel_lab/live_v6_sim_server.py` · `:8770` |
| Research harness | `tmf_channel.harness`（強制 live `PAPER_RECIPE`）· bars SSOT：`$GOLDENSTOCKS_DATA_DIR/cache/tmf_channel/bars.sqlite` |
| Order dry | `tmf_order_translator.py` · `/api/orders-dry` · `tmf_orders_dry.json` |
| 術語 | 見 `docs/terminology.md`；採納規格見 `config/strategy.yaml` · `tmf-micro-channel` |

> **免責**：個人研究紙上規格與本機 infra 藍圖；不構成投資建議。Live 送單須另經 Order layer 閘門（`ORDER_MASTER_ENABLED` 等）。

> **2026-08-06 架構切開（收盤後）**：引擎遷入 `src/tmf_channel/`；launchd 改 KeepAlive worker＋session pool；TX／tick cache 遷出 git tree 至 `GOLDENSTOCKS_DATA_DIR/cache/tmf_channel/`（SQLite day-lazy）；舊 fork 進 `archive/engines/`。

> **2026-08-06 採納**：H2H vs Final v1.1.3（STRICT_TICK/BAR · 25d/83d）PASS → `PAPER_RECIPE` 換 PV16 specialized；硬規則 night climax_up block L,S 不變。

> **2026-08-05 決策**：開盤前修復 B1/B2（§5.0.1）後，**取消**原「連續 ≥3 個交易時段 dry」驗收；**當日即實盤下單**（日盤 08:45 起）。殘餘風險接受，靠四鎖＋`max_lots=1`＋日 API／虧損熔斷＋人工值守控損。

---

## 1. 產品摘要

### 1.1 一句話
在微型臺指近月 1 分 K 上，**空倉雙向掛水平限價**（價量結構帶內），以 **讓利 trail + 結構破壞 + 遠距保護軌** 出場，並在 **強漲／強跌 regime** 時暫停逆勢新掛。

### 1.2 目標與非目標

| 目標 | 非目標 |
|------|--------|
| 日盤＋夜盤連續紙上／未來實盤可運轉 | 預測「哪天大漲夜」 |
| 多空雙向、可審計成交 | 每根 K 對券商狂改價 |
| 對齊限價掛單語意（回測＝實盤意圖） | 用市價進場衝 fill rate |
| P0 先估 API 次數再接 Fubon | 直接把模擬 place 事件 1:1 打 API |

### 1.3 成功指標（建議）

| KPI | Paper 門檻 | Live 門檻（上線後） |
|-----|------------|---------------------|
| 日勝率（日曆日淨＞0） | ≥70%（IS 6–7 月 ~42/43） | ≥55%／滾動 20 日 |
| 夜盤紅日占比 | ≤25% | ≤35% |
| 多空筆數 bal | ≥0.7 | ≥0.6 |
| 委託 place+cancel／日 | dry 節流後觀察 | **≤120**（目標 40–80） |
| 券商對帳漂移 | — | 0 未解釋部位 |

---

## 2. 開盤掛單行為（重要 · 現況 vs 期望）

### 2.1 你期望的
「日盤／夜盤**開盤前**就掛好多＋空限價。」

### 2.2 程式**現況**（2026-08-04 實測）

| 時段 | 實際行為 |
|------|----------|
| **日盤** | 需有 1 分 K 後才掛。今日約 **08:54** 先出現單邊，**08:57** 才雙掛（非 08:45 前）。 |
| **夜盤** | **15:00** 起可掛，但不保證雙邊。今日 **15:00** 先只有多掛；**15:20** 才雙掛（開盤常落在 `expand_up`／`climax_up` → regime 濾網**不掛空**）。 |
| **盤前** | **不會**在 08:45／15:00 之前向交易所預掛（paper 也沒有盤前 bar）。 |

### 2.3 為什麼不是「一定雙掛」
空倉時意圖是雙掛（`in_pos_hang=both` + `open_bias=0`），但會被蓋掉：

1. **`trend_hang_dampen=regime`**：`expand_up`／`climax_up` → 不新掛空；`expand_dn`／`climax_dn` → 不新掛多。  
2. **`skip_quiet_regime`**：`contract`／`dry` 且空倉 → 不新掛。  
3. **`place_every=3`**：約每 3 根才嘗試 PLACE → 開盤後可能晚幾分鐘。  
4. **`gap_bias`（08:45–09:15）**：若 morning_gate 有偏邊，可能單邊。  
5. Sticky 下首掛需完成 bar 與結構價計算。

### 2.4 v1.2 開盤貪心窗（已實作 · 預設 OFF）

引擎參數（`jack_channel_v6_pv.py`，**不改變 Final v1.1 預設**）：

| 參數 | 預設 | 含義 |
|------|------|------|
| `open_greed` | **false** | 總開關 |
| `open_greed_bars` | 15 | 日 08:45／夜 15:00 起 N 根 |
| `open_greed_lo/hi` | 80／120 | 日盤相對 **session open** 的絕對掛距 |
| `open_greed_night_lo/hi` | 40／60 | 夜盤 |
| `open_greed_sides` | both | `both`／`L`／`S` |
| `open_greed_asym` | none | `buy_favor`：空邊 capped（`open_greed_s_cap`） |
| `open_greed_force_place` | true | 窗內空軌每根嘗試 PLACE |
| `open_greed_cancel_unfilled` | true | 窗結束未成交 → `open_greed_expire` |
| `open_greed_bypass_quiet` | true | 窗內可蓋過 quiet skip |

語意：開盤短窗掛「遠一點的彩票限價」，沒成交就撤，回到正常 30–60。  
**仍非券商盤前預掛**（無 08:40／14:55 API）；是第一根可用 1 分 K 起掛。

#### Bake-off（2026-06～07 · 43 日 · `_hhmm` ISO 修正後）

| 配方 | net | Δ vs v1.1 |
|------|-----|-----------|
| v1.1 baseline | **78,696** | — |
| 雙向 greed 80/120 | 76,487 | −2.2k |
| buy_favor | 75,816 | −2.9k |
| **L-only greed 120** | **79,259** | **+0.6k** |
| S-only greed 120 | 75,602 | −3.1k |
| `place_every=1` | **134,314** | **+55.6k** |
| greed + place1 | ~134k | 略遜／持平 place1 |

證據檔：`reports/research/channel_lab/open_greed_v12_bakeoff.json`。

**裁決（研究）**

1. **不要**開雙向／偏空開盤貪心（開盤空單期望偏毒）。  
2. **L-only 遠買彩票**僅微幅正期望，可當可選實驗，不足以單獨升格 Final。  
3. **更強槓桿是 `place_every=1`**（更早／更勤掛），但會推高 API；上 live 前須節流。  
4. 真·盤前 ROD（08:40／14:55）仍屬後續 infra，本節只覆蓋「第一根 K 起貪心」。

### 2.5 `_hhmm` ISO 修正（2026-08-04）

`day_arrays` 使用 `...T08:45:00+08:00` 時，舊 `_hhmm` 誤回 `2026-`，導致**全日被當成夜盤**（`night_hang_scale=0.5`）。  
修正後 Jun–Jul v1.1 net 由先前紙上 ~92k 下修至 ~**79k**。舊 exit_lab 數字若未重跑，勿與新基準直接比。

---

## 3. 策略規格（Final v1.1）

### 3.1 資料
- **頻率**：1 分 K（日盤 + afterhours）。  
- **來源（paper）**：Fubon futopt `intraday.candles`。  
- **商品**：`resolve_front_symbol()` → `product=TMF`，取 `endDate ≥ today` 最近契約。  
- **輔助**：VIXTWN lag1（session bias）、optional morning `gap_bias` JSON。

### 3.2 進場（掛單）

| 參數 | 值 | 說明 |
|------|-----|------|
| `hang_lo` / `hang_hi` | 30 / **38** | 日盤掛距帶（點）· Final v1.1.2 |
| `night_hang_lo` / `night_hang_hi` | **15 / 30** | 夜盤絕對帶（不隨日 hi×0.5） |
| `night_hang_scale` | 0.5 | 僅當夜絕對帶未設時備用 |
| `sticky` | true | 水平限價，不追價 amend |
| `place_every` | 3 | 空軌時嘗試 PLACE 間隔（根） |
| `open_bias_pts` | 0 | 不因相對日開只掛一邊 |
| `in_pos_hang` | both | 持倉仍可掛同向加碼＋對向保護 |
| `max_lots` | 2 | 同向最多 2 口 |
| `skip_quiet_regime` | true | contract/dry 空倉不新掛 |
| `trend_hang_dampen` | **regime** | 見 §3.4 |
| `cross_bar_only` | true | 進場 bar 不對向平倉 |

掛價：結構 pivot（look≈30）落入 band；sticky 出生後水平維持，直到太遠／撤銷／成交。

### 3.3 出場（hybrid_trail）

| 層級 | 條件 | why 標籤 |
|------|------|----------|
| **主** 讓利 trail | 浮盈 MFE≥**50** 後，自高峰回吐≥**40** | `trail|…` |
| **輔** 結構 | 收盤跌破／突破近 **12** 根 swing（PIT） | `struct_break|…` |
| **保護** 遠距對向 | 持倉對向掛在 **80–120**；近軌 `far_sanitize` | 被動成交→`opp_cover`（應稀少） |
| **硬停損** | 逆向≥**150** 且持倉≥**12** 根 | `stop|…` |
| 最短智慧出場 | `min_hold_before_smart=3` | 避免剛進場亂砍 |
| **PI 結構寬限** | gift≥**5** 點時，進場後 **5** 根內不觸發 `struct_break` | `improv_struct_grace_bars=5` |

### 3.4 Regime 順勢濾網（對稱）

空倉且：

- `expand_up` 或 `climax_up` → **不掛空**，並撤已掛空（`dampen_pull`／`dampen_sanitize`）。  
- `expand_dn` 或 `climax_dn` → **不掛多**，對稱撤多。  

PIT：只用當根及以前價量分類（`classify_pv` + rvol）。

### 3.5 持倉與加碼
- 同向 scale-in 最多 2；反向不可同時持有（先平）。  
- `allow_flip=false`：對向觸及＝平倉，不自動反手開倉。

### 3.6 撮合優於限價（price improvement · Formal）

**制度語意**：限價買不會貴於委託價、限價賣不會低於委託價。跳空穿越 resting limit 時，引擎以**開盤價**成交（優於限價）——對齊連續競價常見結果；1 分 OHLC 代理，**非**完整盤前試撮簿。

| 參數 | 預設 | 說明 |
|------|------|------|
| `gap_fill_improve` | **true** | 跳空穿越 → fill@open；false＝強制限價成交（研究對照） |
| `improv_struct_grace_bars` | **5** | gift 進場後 N 根內不跑 `struct_break` |
| `improv_struct_min_pts` | 5 | 視為 gift 的最小優惠點數 |
| `improv_struct_until_trail` | false | 未 arm trail 前也不 struct（bake 後**拒絕**：傷 net） |

成交／持倉審計欄位：`hung`（原限價）、`improv`（優惠點數）；fill note 含 `improv=N`。

#### 證據（2026-06～07 · 43 日）

| 對照 | net | 備註 |
|------|-----|------|
| improve ON · grace=5（正式） | **~78,767** | 42/43 日正 |
| improve ON · grace=0 | ~78,696 | struct gift keep%~28% |
| improve **OFF** | ~58,203 | Δ ≈ **−20.5k**＝gift 質量 |
| open±K harvest 回歸 | 大多負 | 不成獨立策略 |
| until_trail / grace≥8 | 低於 grace0/5 | 拒絕 |

Gift 單依出場：`trail` keep%~70%（主保留通道）；`struct_break` 最會吐 gift（grace0 keep%~28%、早期 hold≤5 近 0%）；`opp_cover` keep%~100%。  
正式規則選 **grace=5**：struct gift 略改善且總 net 微升；長寬限提高 keep% 但總淨利下降。

研究 topic：`tmf-match-price-improvement` · 證據 `tmf_price_improvement_lab.json`。

---

## 4. 回測與壓力（摘要）

| 視窗 | Final v1.1.1（PI retain） | 備註 |
|------|----------------------|------|
| 2026-06～07（43 日）**舊契約** | 約 **+78,567**，42/43 日正 | cache 止於 23:59 + `eod_flatten`（非紙上等價） |
| 同窗 **完整夜盤＋EOD@~05:00** | 約 **+93,549**，42/43 日正 | FinMind tick 補 00:00–04:59；見 §4.1 |
| 同窗 **完整夜盤＋紙上不平** | 約 **+93,392**（+u → **+93,594**） | 與上列幾乎同；最接近 live paper |
| 同窗 improve OFF（舊契約） | 約 **+58,203** | gift 貢獻 ~+20.5k（仍為短夜盤契約） |
| 同窗 `place_every=1` | 約 **+134k**，43/43 | API 成本↑；非 Final 預設 |
| Wide10 / Focus5 | 優於純 opp／純 trail／純 far | 見 `exit_lab_*.json` |
| 夜盤紅日（日曆） | 舊契約約 7/43；完整夜盤同窗紅日仍 **1**（06-03） | 午夜段另計 |
| 2026-08-04 paper | 全日仍可為正；夜盤可紅可翻 | 單日不定生死 |

**限制**：樣本偏 2026 夏；實盤滑價／手續費未完全等價；**無**連續 60 日 live 證據；gap-through ≠ 盤前試撮完整還原；完整夜盤 IS 用 **TX tick→1m** 接在既有 daynight cache（非 TMF 原生 1m）。

### 4.1 評價契約落差（23:59 EOD vs 扛到天亮）· 2026-08-05 補完

| 契約 | 資料 | `eod_flatten` | 語意 |
|------|------|---------------|------|
| **舊回測** | `tx_1m_daynight_cache` **止於 23:59** | `True` → **EOD@23:59** | 短夜盤 |
| **紙上／實盤意圖** | 完整 afterhours **到 ~04:59** | `False` | 可跨午夜 |

**Phase A（10 session · tick_raw）**：`fullnight_eod_gap_lab.json` — A +13,041 → C +14,240（~+9%）。

**Phase B（Jun–Jul 43 session · FinMind TX tick backfill）**：`fullnight_junjul_restatement.json` · cache `tx_1m_fullnight_cache.json`

| 政策 | Net | 日勝率 | 00:00–05:00 進場 |
|------|-----|--------|------------------|
| A 短夜＋EOD@23:59 | **+78,567** | 98%（紅 06-03） | — |
| C 完整夜＋EOD@~05:00 | **+93,549** | 98%（同紅日） | 779 筆／**+14,328** |
| D 完整夜＋不平（紙上） | +93,392（+u **+93,594**） | 98% | +14,171 |

午夜段細節：WR≈**47%**，med≈**−6**，avg≈+18；最差單筆 −543、最佳 +840。相對 A：32 日變好、11 日變差（最差 Δ 約 −517 @06-22）。

**結論**

1. 契約不一致屬實；補齊後 **不是爆倉區**，且 Jun–Jul 總淨 **高出約 +15k（~+19%）**。  
2. 午夜是 **薄邊高換手**（筆數多、勝率近半），不是主引擎也不是系統性毒藥。  
3. 公布／對外請改用 **C 或 D（~+93.5k）** 當紙上等價 IS；舊 +78k 僅作短夜盤對照。  
4. 評價 SSOT：完整 15:00–05:00 + 紙上旗標（D）或真收盤強平（C）。腳本：`fullnight_junjul_restatement.py`。

### 4.2 多 agent 信心驗證兩輪（2026-08-05）

上線前針對「§3 各子步驟有沒有專業驗證背書」跑了兩輪多 agent 補強（round 1：8 項；round 2：5 項），逐項用實際回測／敏感度測試核實，而非僅憑程式碼審查。**注意：以下多數數字仍以舊 +78,767 基準測試，尚未對新 +93.5k 完整夜盤基準重跑；方向性結論應仍成立，絕對數字待覆核。**

| 子步驟 | 信心變化 | 關鍵發現 |
|---|---|---|
| 撮合優於限價（gap_fill_improve） | 低→**高** | 獨立 10 天夜盤窗貢獻佔比（24.9–25.3%）與 43 天窗（26.1%）高度一致，非單窗巧合；但單日（05-27）可佔新樣本全部 delta 的 49%，收益集中於少數大 gap 日 |
| Regime 門檻 CLIMAX/DRY | 低→中 | 跨 ±30% 擾動、跨不同月份（Apr-May 低波動窗）皆一致零貢獻——確認是「跨情境都缺乏鑑別力的自由度」，非 Jun-Jul 偶然；需擾動達 ±50–70% 才微幅觸發 |
| Regime 門檻 CONTRACT | — | round 2 新發現：比原認定更敏感，Apr-May 窗 ±30% 內已 FRAGILE（−13%），未深入查，優先度低但應排入後續健檢 |
| VIX lag1 換盤沖倉 | 低→中 | 舊窗顯示 −1.2%（36 事件），獨立 10 天窗**未重現、翻正**+0.2%（5 事件）——兩窗都是雜訊等級，判斷此機制大機率中性，不建議因此關閉 |
| 交易成本假設 COST=3 | 低→中 | 外部估算合理來回成本 6–11 點，3 點僅為樂觀下限（低估 2–4 倍）；COST8＋VIX關閉疊加，43 天 net 剩 65,539（**對舊 78,767 打 8.3 折**，兩因子近乎線性可加） |
| 出場參數 50/40/12 | 中→**低** | 查證後非聯合網格最優解——35 候選中原僅排第 6，最終靠 8 選 1、短窗 stress test 以 <1% margin 險勝；20+ 候選從未在長窗測過。**兩窗聯合重新排名腳本因運算量過大未跑完**，`50/40/12 是否站得住腳仍無最終答案` |
| 熔斷邏輯端到端 | 中→**高**，但發現新缺口 | 合成測試證實觸發／跨午夜維持／跨 05:00 解除三情境皆正確；**但額外發現：`reconcile_once` 熔斷觸發後完全停止輪詢，而 TMF 無任何券商端停損單——熔斷期間既有部位變成無下限裸倉**。已用窄範圍補丁緩解，見 §5.0.2 |
| Teacher match（jack_channel_v5.py） | 極低→低 | AST 解析確認在現行 `v6_pv.py` 呼叫次數為 0，是死 import，對本策略驗證無實質貢獻，正式驗證應忽略此機制 |

**總結**：兩輪補強後，多數子步驟從「模糊懷疑」變成「明確定性」（部分是壞消息，如出場參數排名、熔斷裸倉風險；部分是好消息，如撮合優於限價的跨窗穩健性、VIX 機制的中性判定）。**未解決的最大缺口**：出場參數兩窗聯合排名沒有跑完，以及本節多數壓力測試尚未對新 93.5k 完整夜盤基準重算。證據檔：`reports/research/channel_lab/{regime_threshold_sensitivity,pi_fullnight_robustness_check,vix_lag1_session_bias_ablation,tmf_cost_sensitivity_check,test_tmf_kill_switch_e2e,exit_lab_two_window_joint_rank,conservative_stack_lab,kill_switch_partial_freeze_prototype}.{py,json}`。

---

## 5. Order 執行藍圖（P0 → Live）

### 5.0 現況（2026-08-05）

| 元件 | 路徑 | 狀態 |
|------|------|------|
| Futopt adapter | `src/order/fubon_futopt_orders.py` | **已接** place/cancel/query + `live_submit_guard` |
| Desired-state poll | `src/order/tmf_channel_order.py` | **已接**（非全日 event 重放）；含 §5.0.2 熔斷平倉補丁 |
| 手動入口 | `scripts/order/run_tmf_channel_poll.py` | `.venv-fubon`；除錯用；正式靠 launchd |
| Sleeve 設定 | `config/order.yaml` · `tmf-micro-channel` | **`enabled: true` · `auto_submit: true`**（2026-08-05 決策改）|
| Ledger | `data/order/tmf_channel_ledger.json` | API／kill／last desired |
| launchd | `com.jackm4.goldenstocks.tmf-channel-poll` | **已安裝 + enabled**（2026-08-06 起 KeepAlive 常駐 worker · 窗內 interval≈20s · session 重用；原 `StartInterval=60` 已退役 · 日／夜時窗改由 worker 內部 sleep 處理，見 §7.2） |
| 採納登錄 | `config/strategy.yaml` + `strategies.yaml` | **已改 `enabled: true`**（2026-08-05） |
| 其餘 order-capable 策略 | `leading-dip-poll`／`songshan-copytrade-poll`／`expert-pool-staged-gate`／`detach-gate` | 全部 `job_registry.yaml status: disabled` + `launchctl print-disabled ⇒ disabled`，無背景 process；**TMF 是下單層目前唯一實際運作的策略** |

**實彈四鎖（必須全開才會 `sdk.futopt.place_order`）**

1. `ORDER_MASTER_ENABLED=1`  
2. `ORDER_TMF_CHANNEL_ENABLED=1`  
3. `ORDER_TMF_CHANNEL_AUTO_SUBMIT=1`  
4. `ORDER_TMF_CHANNEL_DRY_RUN=0`  

任一未開 → 強制 dry（只對帳／印 actions，不送單）。

**開鎖清單（2026-08-05 · 決策：今日實盤）**

- [x] ~~連續 ≥3 個交易時段 dry~~ —— **已取消**（決策覆蓋；不再作為上線阻斷）
- [ ] 開盤前／開盤後首小時：`api_calls_day` ≤120（超則人工停）
- [ ] 抽樣對帳：want rails 與券商委託一致（首批成交後）
- [x] 展期：不適用（到期 2026-08-19，非展期週）
- [x] 熔斷 env 已設：`ORDER_TMF_CHANNEL_KILL_DAY_LOSS` + 日 API cap（邏輯已修；首日實戰觀察）
- [ ] 確認 `ORDER_TMF_ACCOUNT` 指到期貨帳（多帳必填）
- [x] 四鎖全開：`ORDER_MASTER_ENABLED=1` · `ORDER_TMF_CHANNEL_ENABLED=1` · `ORDER_TMF_CHANNEL_AUTO_SUBMIT=1` · `ORDER_TMF_CHANNEL_DRY_RUN=0`
- [x] 安裝並 enable `com.jackm4.goldenstocks.tmf-channel-poll`（2026-08-05 13:51）；已停用手動 day/night loop daemon

**結論：今日實盤。** 原 ≥3 時段 dry 門檻作廢；正式執行路徑為 launchd（開機自動、與研究腳本隔離）。plist 內 `ENABLED=0`／`DRY_RUN=1` 是與其他 order job 相同的 fail-closed 預設，launcher `source .env` 後會覆寫成實彈。

### 5.0.1 2026-08-05 開盤前 bug 修復紀錄

上線前例行驗證（非固定排程，人工於開盤前執行一次 dry poll）意外發現 2 個 Order 層缺陷，兩者皆已修復並補測試（`tests/test_tmf_channel_order.py`，9 test 全過），但**皆未經過一整個交易時段的實戰驗證**：

| # | 缺陷 | 發現方式 | 修復 | 對應風險 |
|---|------|----------|------|----------|
| B1 | `src/order/tmf_channel_marketdata.py` 抓 1m K 時只取 HH:MM、丟棄日期；夜盤資料橫跨日曆日，導致昨晚的 K 被誤標成今晚 | 07:34 dry poll 實測重現：`open_pos.et` 顯示進場時間在**未來**（poll 當下 07:34，回傳進場時間 23:55）| 比照 `live_v6_sim_server.py` 已驗證正確的 `_row_dt`／`_in_night_window`／`_in_day_window` 邏輯，保留每根 K 自己的日期，不再統一貼「今天」| R9.1（新增，見風險登記） |
| B2 | `src/order/tmf_channel_ledger.py` 的 `roll_day()` 用日曆午夜重置 `killed`／`api_calls_day`／`broker_pos`；夜盤橫跨午夜，熔斷會在半夜 00:00 被靜默解除，即使夜盤仍在進行。另外熔斷只檢查目前持倉的浮動損益，從未真的累加已實現損益 | 程式碼審查（非實測重現；門檻 400pt 在審查當下未被觸發） | 新增 `trading_day_str()`：00:00–04:59 歸屬前一晚交易日；`day_pnl_pts` 改為每次 poll 用當前 bar 視窗內所有已實現交易重算（非累加），熔斷改為「累計已實現虧損」或「單倉浮動虧損」任一觸發即熔斷 | R9.2（新增，見風險登記） |

**這兩個修復本身沒有經過完整交易時段驗證**——今天開盤（08:45）是修復後第一次真實資料曝光。決策已接受此風險並**取消**「≥3 時段 dry」阻斷；首日以 1 lot、API／虧損熔斷、人工值守為控損。

### 5.0.2 熔斷「裸倉」缺口與窄範圍補丁（2026-08-05 開盤前）

**發現**：§4.2 多 agent round 2 對熔斷邏輯做端到端合成測試時，額外發現一個既有（非今日修復引入）的結構性缺口——`reconcile_once()` 一旦 `ledger["killed"]=True`，會在函式最頂端直接 return，完全跳過查券商／查持倉／管理既有部位的邏輯，直到隔天 05:00 熔斷解除才恢復。查證 `src/order/fubon_futopt_orders.py` 確認 **TMF 下單只有限價／市價兩種，完全沒有券商端停損單**——所有 trail／structure／stop_pts 保護都是純軟體邏輯，只在 `reconcile_once` 實際跑的時候才會生效。也就是說：**熔斷觸發當下如果手上有部位，那個部位會變成完全無防護的裸倉，直到隔天早上**，理論上虧損無上限（合成測試情境下比正確設計多虧 650 點，且無實際上限）。

**決策**：不做完整的「熔斷時只擋新倉、既有部位仍正常出場」重構（round 2 的原型設計尚未成熟，未覆蓋孤兒部位路徑、未用真實 `simulate()` 覆核），改用**窄範圍、風險更低的臨時補丁**：熔斷觸發當下，若查到券商仍有淨部位，立刻送一次市價平倉（IOC close），其餘邏輯（包含後續每次 poll 仍會嘗試查詢並補平，具備自我重試特性）維持凍結。

**實作**：`src/order/tmf_channel_order.py` 的 `killed` 分支內，新增：`connect_fubon` → `query_tmf_broker_net`（真實查詢當前券商淨部位）→ 若有部位則 `place_futopt_order(..., price_type="market", time_in_force="ioc", order_type="close", dry_run=cfg.dry_run)`。全程包在 `try/except`，任何連線／查詢失敗只記錄 `kill_flatten_error`，不會讓 poll 整支崩潰；仍完全受既有 `dry_run` fail-closed 鏈保護（四鎖任一未開＝只查詢不送單）。

**驗證**：`tests/test_tmf_channel_order.py` 新增 `KillSwitchFlattenStopgapTest`（3 個測試，皆用 mock 隔開真實 Fubon 連線）：① 有裸倉時正確送出一次市價平倉、方向／口數／單別皆對 ② 已無部位時不動作 ③ 連線失敗時不崩潰、正確回報錯誤。全部套件 12 test 通過。**尚未在真實下單模式下實際觸發過**（今日 `killed` 目前為 `false`）。

**仍是待辦（未含在本次補丁範圍）**：round 2 提出的完整「只擋新倉、仍管理既有部位」重構——需要用真實 `simulate()` 逐 bar 覆核 want_s/want_l 語意、覆蓋 `flatten_why`（孤兒／超額部位）路徑、補整合測試，才可視為成熟可上線的版本。

### 5.1 P0 dry translator（paper UI）

- 端點：`GET /api/orders-dry`；檔：`tmf_orders_dry.json`。  
- 用途：估算 API；**live 不走 event 重放**，走 §5.0 reconciler。

### 5.2 委託政策

- 進場／保護：limit ROD  
- 主動出場：IOC／market close（sim flat 且 ledger 有倉）  
- 不做 amend（cancel+place）  
- Live 預設 `max_lots=1`、`place_every=5`、日 API cap 120  

### 5.3 架構

```
Fubon futopt 1m ──► tmf_channel_order.reconcile_once
                      ├─ simulate Final v1.1.1
                      ├─ want_s / want_l / open_pos
                      ├─ get_order_results → diff
                      └─ place/cancel (dry or live via fubon_futopt_orders)
```

### 5.4 單別政策（定案）

| 動作 | 單別 | TIF |
|------|------|-----|
| 進場／加碼 | **限價** | ROD |
| 遠距保護 | **限價** | ROD（節流重掛） |
| Trail／結構出場 | **限價 IOC** 追價 | IOC（v1 reconciler：sim flat → market close） |
| 停損兜底 | IOC 失敗 → **市價** | IOC |
| 改價 | **不做 amend**；必要才撤換 | — |

**市價不作為進場主力**；只作出場確定性兜底。

### 5.5 API 次數
- 舊 event 重放可達 ~500–700／日 → **live 改 desired-state**，日 cap 預設 **120**。  
- Paper UI `/api/orders-dry` 仍估事件量；與 live reconciler 分開。

### 5.6 成交追蹤
1. 送單 → ledger `actions_tail` + 日 API 計數。  
2. Poll `get_order_results` working→filled／cancel。  
3. `broker_pos` ↔ sim `open_pos`；sim flat 則平倉。  
4. 對帳券商未平倉；ambiguous 禁加碼（後續強化）。  

**2026-08-05 實盤事件**：日盤開盤前巡邏（08:36 唯讀連線驗證）查到券商真實淨部位為**空單 3 口 @43791.67**，超過 `max_lots=1` 上限、本地 ledger／sim 皆顯示無倉（孤兒部位）。使用者確認為刻意預期的自動清理，交由既有 `flatten_why="broker_over_max"` 邏輯（§5.0 架構圖，非本次新增）於日盤開盤後自動偵測並平倉，未人工介入下單。

### 5.7 分層邊界
- Lab **不** import `src/order/`；Order poll **可**載入 lab 引擎（路徑注入）。  
- 預設 `ORDER_MASTER_ENABLED=0` + TMF 四鎖（plist fail-closed；實彈只靠 `${GOLDENSTOCKS_DATA_DIR}/.env`）。  
- 正式排程：**launchd `tmf-channel-poll` 已安裝且 enabled**（2026-08-06 起 **KeepAlive 常駐 worker**，窗內 ~20s 對帳、窗外 idle 60s；原 `StartInterval=60` 冷登入已退役，見 §7.2）。禁止另起 nohup／手動 daemon，以免雙跑。

---

## 6. 60 日自主運轉 · 評價與信心

### 6.1 評價（誠實）

| 維度 | 評分（1–5） | 說明 |
|------|-------------|------|
| 研究完整度 | 4 | 出場／濾網／商品近月／P0 dry 已串 |
| 樣本外穩健 | **2.5–3** | IS 強；live／跨季未驗證 |
| 工程可運維 | 3 | Paper 可跑；API 節流與期貨 adapter 未完成 |
| 與「開盤前雙掛」一致性 | **2** | 現況開盤後才掛，且常單邊 |
| 總信心（60 日全自動實盤） | **中偏低** | 可 paper 60 日；**不建議**無值守實盤 60 日 |

**結論**：作為 **研究定稿＋紙上連續跑** 有信心；作為 **無人實盤 60 日** 信心不足，除非完成 Order 節流、成交對帳、展期、熔斷。

### 6.2 日盤／夜盤時鐘（程式已知）

| 時段 | 時間（臺北） | 行為 |
|------|--------------|------|
| 日盤 | 08:45–13:45 | 收 1m、模擬／未來下單 |
| 夜盤 | 15:00–次日 05:00 | afterhours；`night_hang_lo/hi=15/30`（v1.1.2） |
| 中場 | 13:45–15:00 | 無新 bar；維持／平倉政策另定 |

### 6.3 日曆 · 必調事項（60 日）

| 日期／事件 | 動作 |
|------------|------|
| **每交易日** | 確認 paper／worker 存活；看 `/api/orders-dry` 次數；對帳若已 live |
| **契約到期週**（近月 `endDate` 前 3–5 日） | **展期**：減倉舊月、改掛次月；禁止跨月混亂部位。現近月 **2026-08-19** |
| **結算日當日** | 降槓桿或只平不開；避免最後小時結構假突破 |
| **電子／股票期貨結算週、重大選擇權週** | 波動放大：可暫收緊 `max_lots=1`、或提高 trail_arm |
| **長假前最後夜盤／後首日** | 流動性差：考慮 `night_entries=false` 或只出場 |
| **Fubon／行情維護窗** | 停掛；已掛單人工或規則撤 |
| **API 錯誤率升高** | 熔斷：只允許停損平倉 |

### 6.4 每日注意清單（操作者）

1. 進程是否活著（`:8770` / 未來 launchd）。  
2. `symbol` 是否仍為預期近月（展期後）。  
3. 夜盤是否異常連續 `struct_break` 虧損（可暫關夜盤進場）。  
4. dry／live 委託次數是否爆量。  
5. 部位是否與券商一致。  
6. 單日虧損熔斷（建議自訂：例如 −X 點停當天新開）。

### 6.5 建議的 60 日路徑（非一次實彈）

| 日 | 模式 |
|----|------|
| D1–D14 | Paper only + 收緊 translator 至 &lt;120 API／日 |
| D15–D30 | Fubon **dry-run** 送單＋成交 poll（真連、假扣款若 SDK 支援） |
| D31–D45 | Live **1 lot**、僅日盤或僅限價進場 |
| D46–D60 | 視對帳與滑價再開夜盤／2 lot |

---

## 7. 系統架構

```
Fubon futopt 1m (day+AH) ──► live_v6_sim_server
                                │
                                ├─ resolve_front_symbol(TMF)
                                ├─ jack_channel_v6_pv.simulate (Final v1.1)
                                ├─ HTML dashboard :8770
                                └─ tmf_order_translator.translate_dry
                                        │
                                        ├─ /api/orders-dry
                                        └─ tmf_orders_dry.json
                                                │
                                    （未來）order-intent-v1 → Order layer → Fubon
```

### 7.1 關鍵檔案

| 檔 | 職責 |
|----|------|
| `src/tmf_channel/causal_engine.py` | 策略狀態機（引擎 **SSOT**；lab `hang_anchor_causal_lab.py`／舊 `jack_channel_v6_pv.py` 僅 shim／archive） |
| `src/order/tmf_channel_pv16_book.py` | 16-cell recipe book（`RECIPE_VERSION` SSOT；SPECIALIZED＋CELL_TUNE_V2 疊加） |
| `live_v6_sim_server.py` | Paper 服務、近月、API |
| `tmf_order_translator.py` | P0 節流委託計畫 |
| `exit_lab_*.json` / `trend_dampen_bakeoff` | 研究證據 |
| `docs/order-layer-prd.md` | 全庫 Order 層總 PRD |

### 7.2 v1.2.0+ 執行架構（2026-08-06 · 16:03 cutover 上線）

上圖為 Paper UI 資料流；**live 下單路徑**自 2026-08-06 起改為 launchd KeepAlive 常駐 worker（取代 `StartInterval=60` 每分鐘冷登入）：

```
launchd KeepAlive (com.jackm4.goldenstocks.tmf-channel-poll)
  └─► launcher（lockdir 防雙跑 · source .env 四鎖）
        └─► scripts/order/run_tmf_channel_worker.py
              └─► tmf_channel.worker_loop（窗內 ~20s／窗外 idle 60s，內部 sleep 不退出）
                    ├─ tmf_channel.session_pool（單次 Fubon 登入 · 跨輪重用）
                    └─ tmf_channel_order.reconcile_once
                          ├─ src/tmf_channel/causal_engine.py（desired-state：want_s/want_l/open_pos）
                          └─ fubon_futopt_orders place/cancel（四鎖 · 任一未開＝dry）
```

- **Recipe**：`recipe_version = final_v1_3_0_pv16_celltune_v2`；cell book 在 `src/order/tmf_channel_pv16_book.py`（`SPECIALIZED_PATCHES` 先套、`CELL_TUNE_V2_PATCHES` 後套、後者優先）。
- **引擎 SSOT**：`src/tmf_channel/`（`causal_engine.py`＋`harness.py`）；禁再 import `reports/` lab 引擎或 fork。
- **部署**：`scripts/order/tmf_cutover.sh`（preflight import 檢查 → `launchctl kickstart -k` → 等首輪對帳；停機 < 5 秒）。
- **手動除錯**：`scripts/order/run_tmf_channel_poll.py --json` 單次；勿與 worker 並跑、勿 `--force`（見 `.cursor/rules/tmf-channel-single-path.mdc`）。

---

## 8. 風險登記

| ID | 風險 | 緩解 |
|----|------|------|
| R1 | 開盤非雙掛／非盤前 | v1.2 open_greed（預設 OFF）；真盤前 ROD 另案 |
| R2 | API 次數過高 | 加嚴 far 節流；慎開 `place_every=1` |
| R3 | 夜盤結構出場過敏 | 可調 `struct_exit_look` 或夜盤關進場 |
| R4 | 展期錯月 | 到期日曆＋自動 symbol |
| R5 | 回測≠實盤滑價 | 限價進場；IOC 記錄實際成交 |
| R6 | 無人實盤爆倉 | 熔斷、1 lot、MASTER=0 預設 |
| R7 | dampen+sticky 曾漏撤 | 已修 sanitize（須回歸測試） |
| R8 | `_hhmm` ISO 曾全誤判夜盤 | 已修；舊 net 數字需重標 |
| R9 | PnL 依賴 gap-through 優於限價 | Formal `gap_fill_improve`；OFF 約 −20k；§3.6 |
| R9.1 | Order 層 marketdata 曾丟棄日期，夜盤跨日曆日資料被誤標（實測重現：`open_pos.et` 進場時間顯示在未來） | 2026-08-05 已修（`tmf_channel_marketdata.py` 比照 paper 版日期判斷邏輯）；**尚無交易時段實戰驗證** |
| R9.2 | 熔斷用日曆午夜重置（夜盤跨午夜會被靜默解除）；且原本只看單倉浮動損益，從未累加已實現虧損 | 2026-08-05 已修（`trading_day_str()` session 邊界 + `day_pnl_pts` 改累計已實現）；**尚無觸發／未觸發的實戰驗證** |
| R10 | 修復 B1/B2 未經完整交易時段驗證即上線 | **已接受**：取消 ≥3 時段 dry；2026-08-05 今日實盤；靠 1 lot／熔斷／值守 |
| R11 | 熔斷「整段凍結」＝裸倉無下限風險（TMF 無券商端停損單，見 §5.0.2） | 2026-08-05 已用窄範圍 flatten 補丁緩解（觸發即市價平倉，未經實戰觸發驗證）；完整「只擋新倉」重構仍待辦 |
| R12 | 出場參數 50/40/12 未證實為聯合最優（35 候選中原僅排第 6，兩窗聯合重排未跑完） | 維持現行值（無更優候選證據）；待補跑 `exit_lab_two_window_joint_rank.py`（需更長執行時間或分批） |
| R13 | Regime 門檻 CLIMAX/DRY 跨情境（±30%、跨月份）皆為死參數 | 非過擬合證據，但也非必要自由度；不影響現行運行，記錄供未來簡化參考 |
| R14 | Regime 門檻 CONTRACT 比預期敏感（Apr-May 窗 ±30% 內已 FRAGILE −13%） | 尚未深入查，優先度低，排入後續健檢 |
| R15 | 回測方法論兩個未來函數（收盤價 C[t] 定價 rail 卻對照同根更早的 tick；tick-native 加碼餓死）修正後，83天真實 **TX** tick 驗證顯示**連現行 max_lots=1 都是負期望值**（−13,520pt／44.6%勝天／maxDD −14,477） | **未緩解，初步發現未經獨立複驗**：`reports/research/channel_lab/H-LOT-FIXED2_and_v112_tick_native_validation.md`；資料為 **TX 大台**（未來正式商品；現行 TMF 僅暫時實驗，不必改抓 TMF tick）；VIX盤別邊界出場等次要路徑尚未改tick-native；需要獨立複驗或至少排入下一輪健檢再決定是否影響實盤／未來 TX |

---

## 9. 驗收清單（上 live 前）

- [ ] `/api/state` 顯示近月 symbol＋到期日  
- [ ] Final v1.1 recipe 欄位齊（hybrid＋regime）  
- [ ] dampen 強漲時 resting short 為空（抽測）  
- [ ] `/api/orders-dry` 或 live ledger `api_calls_day` **≤120**（首日緊盯）  
- [x] 期貨 Order adapter + ledger poll 單測（9 tests）  
- [ ] 展期演練（H6→I6；到期前另做）  
- [ ] 單日虧損／API 錯誤熔斷 —— 邏輯已修；**首日實盤觀察**  
- [x] 熔斷裸倉風險（R11）—— 窄範圍 flatten 補丁已上、12 test 通過；**未經實戰觸發驗證**，完整重構待辦  
- [x] ~~≥3 時段 dry~~ —— **已取消**（2026-08-05 決策）  
- [ ] 操作者值守（至少首日日＋夜盤）

---

## 10. 修訂紀錄

| 版 | 日期 | 變更 |
|----|------|------|
| v1 | 2026-08 | hybrid50／gb40 定稿 |
| v1.1 | 2026-08-04 | 對稱 regime 濾網；dampen sticky 修復 |
| v1.1 front | 2026-08-04 | TMF 近月自動解析；P0 order dry |
| 本白皮書 | 2026-08-04 | 開盤行為落差、60 日運維、Order PRD 對接 |
| v1.2 research | 2026-08-04 | `open_greed*` 實作（預設 OFF）；L-only 微正、雙向負；`_hhmm` ISO 修正 |
| **v1.1.1 PI** | **2026-08-05** | Formal：`gap_fill_improve` + `improv_struct_grace_bars=5`；gift 歸因；白皮書 §3.6 |
| **Order live wire** | **2026-08-05** | futopt adapter + desired-state poll + 四鎖；同日午後改 **launchd enabled**（停手動 daemon） |
| **Order bug fix** | **2026-08-05 開盤前** | B1 marketdata 日期丟失（實測重現＋修復）、B2 熔斷日界／累計損益邏輯錯誤（審查發現＋修復）；9 test 全過 |
| **Go-live today** | **2026-08-05** | **取消** ≥3 交易時段 dry 門檻；決策今日實盤開四鎖（§5.0／§11） |
| **v1.1.2 D38** | **2026-08-05** | 日 `hang_hi=38`／夜絕對 `15–30`（`night_hang_lo/hi`）；Order `PAPER_RECIPE`＋strategy frozen_id；研究 `tmf-hang-hi-day-tighten` |
| **Fullnight Jun-Jul restatement** | **2026-08-05** | FinMind TX tick 補 00:00–04:59；43 天基準由 +78,567 改報 **+93,549／+93,392**（§4.1） |
| **Confidence round 1+2** | **2026-08-05** | 兩輪多 agent 驗證 13 個子步驟（§4.2）；出場參數排名下修為低信心、撮合優於限價上修為高信心 |
| **Kill-switch flatten stopgap** | **2026-08-05 開盤前** | 發現 TMF 無券商端停損單、熔斷全凍結＝裸倉無下限風險（R11）；窄範圍補丁上線 + 3 test（§5.0.2） |
| **口數規劃 tick-native 驗證** | **2026-08-05** | 研究 max_lots=1 vs 2 過程中，修正兩個回測未來函數 bug，初步發現連現行 max_lots=1 都是負期望值（R15，見 `reports/research/channel_lab/H-LOT-FIXED2_and_v112_tick_native_validation.md`）；**未經獨立複驗，未改動實盤** |
| **v1.2.0 架構切開 · cutover** | **2026-08-06 16:03** | 引擎凍結遷入 `src/tmf_channel/`（`causal_engine.py` SSOT，lab 檔改 shim）；launchd 改 **KeepAlive 常駐 worker**（`run_tmf_channel_worker.py` · session 重用 · 窗內 ~20s），退役 `StartInterval=60` 冷登入；`scripts/order/tmf_cutover.sh` 一鍵部署（§7.2） |
| **v1.3.0 celltune v2** | **2026-08-06** | `recipe_version` → **final_v1_3_0_pv16_celltune_v2**：`src/order/tmf_channel_pv16_book.py` 於 SPECIALIZED 之上疊加 `CELL_TUNE_V2_PATCHES`（後套者優先） |

---

## 11. 給決策者的一段話

Final v1.1.1 已接進 Order 層（futopt + desired-state reconciler）。開盤「遠掛等回歸」不成獨立策略。

**2026-08-05 決策**：取消「≥3 個交易時段 dry」阻斷，**今日實盤下單**。B1/B2 已修、單測過，但屬修復後首日曝光——風險已知並接受；控損靠四鎖開通、`max_lots=1`、日 API／虧損熔斷、人工值守。開鎖步驟見 §5.0 清單（改 `.env` 四鎖；本 PRD 變更**不會**自動送單）。

**開盤後補充**：兩輪多 agent 驗證（§4.2）+ FinMind 完整夜盤重報（§4.1，基準改為 ~93.5k）之後，又發現並修好一個既有的結構性缺口——熔斷觸發後原本會讓既有部位變成無防護裸倉（R11，§5.0.2），已用窄範圍市價平倉補丁緩解並測試。**剩餘最大未解缺口**：出場參數 50/40/12 的兩窗聯合排名沒有跑完，站不站得住腳仍無最終答案；多數壓力測試（成本／VIX 疊加）仍以舊 78,767 為基準，未對新 93.5k 重算。目前下單層唯一實際運作的策略是 TMF micro-channel，其餘 order-capable job 皆確認為 disabled、無背景 process。

**2026-08-05 當日再補充（R15）**：原本要回答「max_lots 要不要開 2」，往下查證時在既有的 1 分 K 回測方法論裡發現兩個未來函數（出場用收盤價而非盤中觸價；掛單定價用這根 K 棒還沒發生的收盤價 C[t]，卻對照同一根 K 棒更早的真實 tick 判斷成交）。用完整 83 天真實 FinMind tick 資料重建、修正兩個 bug 後的 tick-native 引擎顯示：**連現行 `max_lots=1` 都是負期望值**（83天 −13,520pt、44.6% 勝天、maxDD −14,477pt；固定 2 口版本更負）。這只是初步發現，同一輪過程中我自己就先後做出兩次「看起來可信但其實有 bug」的正期望值結果，尚未經獨立複驗，也還沒把 VIX 盤別邊界出場等次要路徑改成 tick-native。**這件事目前沒有觸發任何自動熔斷或停止動作**——是否要暫停、縮小或維持現行實盤，是需要 jack 決定的事，細節見 `reports/research/channel_lab/H-LOT-FIXED2_and_v112_tick_native_validation.md` 與 R15。
