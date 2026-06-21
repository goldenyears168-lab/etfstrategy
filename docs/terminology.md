# Terminology · 術語規範 SSOT

| Field · 欄位 | Value · 內容 |
|--------------|--------------|
| Version · 版本 | terminology-v2 |
| Status · 狀態 | **Living doc** — aligned with `config/` outputs and report language |
| Agent rule · Agent 規則 | `.cursor/rules/terminology.mdc` |
| Audit · 清障 | [terminology-audit.md](./terminology-audit.md) |

## How to read this document · 閱讀約定

1. **Identifiers**（`strategy_id` · YAML keys · module names）remain **English** in code and config.
2. Each entry gives **paired academic terms** in English and Chinese, each with a **formal definition**—not a translation gloss.
3. **Product layers** (`facts` → `regime` → `research` → `strategy`) are distinct from `src/` L0–L5, VCP funnel screen stages, and infra SOP (`daily-operations.md`).
4. First mention in user-facing prose: **`English term（中文術語）`**, e.g. `Trend posture（趨勢姿態）`.

---

## 1. Core principles · 核心原則

### 1.1 Point-in-time (PIT) · 時點一致性（無前視）

**Definition (EN):** On signal date *T*, any feature, label, or decision rule may use only information whose timestamp is ≤ *T* (typically through *T*−1 close for same-day execution studies). Violations constitute **lookahead bias**.

**定義（中文）：** 在訊號日 *T*，任何特徵、標籤或決策規則僅能使用時間戳 ≤ *T* 的資訊（同日出場研究通常至 *T*−1 收盤）。違反者構成**前視偏差（lookahead bias）**。

**Contrast · 對照:** Distinguish PIT research from **production screen** (live candidate lists) and **live P&L** (實盤損益).

---

### 1.2 Ex-post analysis · 事後分析

**Definition (EN):** Analysis conducted after outcomes are realized, using full sample paths only for **evaluation**—not for rules available at decision time unless explicitly walk-forward or out-of-sample.

**定義（中文）：** 在結果實現後進行的分析；完整樣本路徑僅用於**評估**，除非明示 walk-forward 或樣本外（OOS），不得作為決策當下可得規則。

---

### 1.3 Value for Proof (VFP) · 可驗證交付物

**Definition (EN):** The layer-specific **deliverable** that proves the layer fulfilled its contract: persisted files, database state, or documented decisions—not process narration (“ran the script”, “research progressed”).

**定義（中文）：** 各產品層用以證明履約的**可驗證交付物**：持久化檔案、資料庫狀態或成文決策——而非流程敘述（「跑過腳本」「研究有進展」）。

| Layer · 層 | SSOT | VFP · 可驗證交付物 |
|------------|------|-------------------|
| **Facts layer · 事實層** | `etf-daily` · `layer: facts` | `reports/daily/etf-daily/daily_brief.md` after 16:30; universe denominator = `ETF_CODES_LISTED` (6 listed ETFs; `00407A` optional pre-listing) |
| **Regime layer · 體制診斷層** | `regime-daily` · `config/regime.yaml` | `reports/daily/regime/daily_brief.md` — Regime four-axis diagnostic; **not** a live gate |
| **Research layer · 研究層** | `config/research.yaml` | Artifacts under `reports/research/`; **not** written to strategy SSOT until graduation |
| **Strategy layer · 策略層** | `config/strategy.yaml` | Frozen `strategies.*.backtest` spec + evidence JSON; optional launchd screen; **no** ensemble weighting |

---

### 1.4 Registry `enabled` · 登錄啟用旗標

**Definition (EN):** Boolean in both `config/strategy.yaml` and `config/strategies.yaml` for the same `strategy_id`; controls registry visibility and launchd gating. Must stay aligned across both files.

**定義（中文）：** 同一 `strategy_id` 在 `config/strategy.yaml` 與 `config/strategies.yaml` 的布林旗標；控制產物登錄可見性與 launchd 閘門。兩檔必須一致。

---

## 2. Product layers · 產品分層

### 2.1 Facts layer · 事實層

**Definition (EN):** The product layer that publishes **observable holdings diffs** (L1 share changes across tracked ETFs) without scoring, ranking, or stock selection—descriptive record only.

**定義（中文）：** 發布**可觀測持股差分**（追蹤 ETF 之 L1 股數變化）的產品層；僅作描述性紀錄，不含評分、排序或選股。

**Identifier:** `etf-daily` · **Output · 產物:** `reports/daily/etf-daily/daily_brief.md`

---

### 2.2 Regime layer · 市場體制診斷層

**Definition (EN):** The product layer that publishes a **multi-axis market-structure diagnostic** of the Taiwan equity benchmark and research universe—descriptive context for reading Facts and Strategy outputs; **not** alpha and **not** an automated exposure gate.

**定義（中文）：** 發布台股基準與研究宇宙**多軸市場結構診斷**的產品層；供解讀事實層與策略層產物之環境脈絡；**非** alpha 來源，**非**自動曝險閘門。

**Identifier:** `regime-daily` · **Config · 設定:** `config/regime.yaml`

---

### 2.3 Research layer · 探索性研究層

**Definition (EN):** The product layer for **exploratory** work—parameter sweeps, hypothesis matrices, and literature calibration—before specs are frozen. SSOT: `config/research.yaml` (`topics.*`).

**定義（中文）：** **探索性**工作之產品層——參數掃描、假說矩陣、文獻校準——於規格凍結前。SSOT：`config/research.yaml`（`topics.*`）。

**Graduation · 採納 graduation:** Promotion of a validated topic into frozen entries in `config/strategy.yaml`（採納後寫入策略 SSOT）.

---

### 2.4 Strategy layer · 採納策略規格層

**Definition (EN):** The product layer of **adopted, frozen** strategy specifications—backtest parameters, evidence JSON paths, optional production screens—maintained in parallel without ensemble weighting.

**定義（中文）：** **已採納、已凍結**策略規格之產品層——回測參數、證據 JSON 路徑、可選實盤篩選——多軌並行，無 ensemble 加權。

**SSOT:** `config/strategy.yaml` · **Registry · 產物登錄:** `config/strategies.yaml` (`layer: strategy`)

---

## 3. Regime four-axis diagnostic · 四軸市場體制診斷

**Regime four-axis diagnostic · 四軸市場體制診斷**

**Definition (EN):** The Regime layer’s standardized report framing four **orthogonal descriptive axes** of aggregate market structure: participation breadth, benchmark trend stage, relative rotation, and Stage-2 template participation.

**定義（中文）：** 體制診斷層之標準報告框架，從四個**正交描述軸**刻畫整體市場結構：參與廣度、基準趨勢階段、相對輪動、第二階段模板參與度。

**Use · 使用:** Report title and section headers.  
**Do not use · 勿用:** 「四格雷達」 alone without English; do not treat as buy/sell signal（非買賣訊號）.

**Config · 設定:** `config/regime.yaml` → `axes.*`

---

### 3.1 Breadth zone · 廣度區間

**Definition (EN):** A **categorical partition** of *market breadth*—the percentage of a defined equity universe with close above a specified simple moving average (50-day or 200-day)—into ordered zones from oversold to overbought, following StockCharts / TradingView reference levels.

**定義（中文）：** 對**市場廣度**——定義標的宇宙中，收盤價高於指定簡單移動平均線（50 日或 200 日）之占比——依 StockCharts／TradingView 參考水位劃分為由超賣至過熱的有序**區間類別**。

**Identifier:** `breadth_zone_200` (primary); `breadth_zone_50` · **Module:** `market_breadth_ma`  
**Lineage:** Zweig · Deemer · McClellan **breadth** tradition

| Zone · 區間 | EN label | 中文標籤 | Threshold · 閾值 (% above MA) |
|-------------|----------|----------|----------------------------------|
| `oversold` | Oversold | 超賣 | < 20 |
| `weak` | Weak | 偏弱 | 20 – 40 |
| `neutral` | Neutral | 中性 | 40 – 60 |
| `strong` | Strong | 強勢 | 60 – 80 |
| `overbought` | Overbought | 過熱 | ≥ 80 |

**Related · 相關**

- **50−200 breadth gap · 50−200 廣度差:** Difference between 50-day and 200-day breadth percentages; negative values indicate short-horizon participation below long-horizon breadth（短線參與低於長線廣度）.
- **Breadth divergence · 廣度背離:** Diagnostic flag when the index rises over ~20 sessions while 50-day breadth materially falls (Zweig-type structural divergence); **not** a standalone trade rule.
- **Breadth impulse · 廣度推力:** Event-layer metrics (Zweig Breadth Thrust · Deemer BAM on advance/decline volume) supplementing MA **level** breadth; config: `config/regime.yaml` → `breadth.impulse`.
- **Zweig EMA rhythm tier · Zweig EMA 節奏分級:** Diagnostic tier on adv/decl 10-day EMA (off / low / mid / high); config: `config/regime.yaml` → `breadth.rhythm.tiers`. Regime reports tier only — **not** Strategy overlay exposure.

**Do not use · 勿用:** Colloquial 「強勢／過熱」 without naming the axis **Breadth zone** when referring to 200MA five-zone readings.

---

### 3.2 Trend posture · 趨勢姿態

**Definition (EN):** The Regime axis summarizing **benchmark trend structure**, mapped primarily from **Weinstein Stage Analysis** on weekly bars of the benchmark index (IX0001), optionally cross-checked with **Minervini Trend Template** pass count on daily bars.

**定義（中文）：** 體制診斷軸之一，概括**基準指數趨勢結構**；主要依 **Weinstein 階段分析**（威斯坦階段分析）對基準指數（IX0001）週線判定，並可輔以 **Minervini 趨勢模板**（米涅爾維尼趨勢模板）日線八項通過數交叉驗證。

**Identifier:** `trend_posture` · **Module:** `stage_analysis`  
**Lineage:** Weinstein (1988) *Stage Analysis* · Minervini (2013) *Trend Template*

**Weinstein stage · Weinstein 階段** (weekly · 週線)

| Stage · 階段 | EN | 中文 | Meaning · 涵義 |
|--------------|-----|------|----------------|
| 1 | basing | 築底 | Base-building after decline |
| 2 | advancing | 上升 | Primary uptrend above rising long MA |
| 3 | topping | 築頂 | Distribution / topping |
| 4 | declining | 下跌 | Primary downtrend |

**Do not use · 勿用:** `regime_name` · **Trend regime** (deprecated identifiers).

---

### 3.3 RRG rotation · 相對輪動（RRG）

**Definition (EN):** The Regime axis describing **relative rotation** of individual stocks versus a benchmark using **Relative Rotation Graphs (RRG)**: each name is classified into a quadrant by **RS-Ratio** and **RS-Momentum** (JdK RS metrics, WMA-smoothed implementation).

**定義（中文）：** 體制診斷軸之一，以**相對輪動圖（RRG）**描述個股相對基準之**相對輪動**；依 **RS-Ratio（相對強度比率）** 與 **RS-Momentum（相對強度動量）**（JdK RS 指標，WMA 平滑實作）將標的劃入四象限。

**Identifier:** `rrg_rotation` · **Module:** `rrg_rotation`  
**Lineage:** de Kempenaer (2006–) **Relative Rotation Graphs**

| Quadrant · 象限 | EN | 中文 | Condition · 條件（簡化） |
|-----------------|-----|------|------------------------|
| Leading | Leading | 領先 | RS-Ratio > 100 and RS-Momentum > 100 |
| Weakening | Weakening | 轉弱 | RS-Ratio > 100 and RS-Momentum ≤ 100 |
| Lagging | Lagging | 落後 | RS-Ratio ≤ 100 and RS-Momentum ≤ 100 |
| Improving | Improving | 改善 | RS-Ratio ≤ 100 and RS-Momentum > 100 |

---

### 3.4 Stage-2 participation · 第二階段參與度

**Definition (EN):** The Regime axis measuring the **cross-sectional participation rate**: the fraction of the research universe meeting Minervini **Trend Template** criteria indicative of Stage-2 uptrends on the evaluation date.

**定義（中文）：** 體制診斷軸之一，度量**橫截面參與率**：評估日研究宇宙中，符合 Minervini **趨勢模板**（具 Stage 2 上升特徵）之標的占比。

**Identifier:** `stage2_participation` · **Module:** `stage_analysis`

---

## 4. Research & backtest terms · 研究與回測術語

### 4.1 Research topic · 研究主題

**Definition (EN):** A bounded exploratory unit in `config/research.yaml` (`topics.*`) with scripts, report directory, and optional `graduated_strategy` link—status `active` or `archived`.

**定義（中文）：** `config/research.yaml`（`topics.*`）中之 bounded 探索單元，含腳本、報告目錄及可選 `graduated_strategy` 連結；狀態為 `active` 或 `archived`。

---

### 4.2 Parameter sweep · 參數掃描

**Definition (EN):** Systematic grid or range search over strategy or model hyperparameters to map sensitivity before adoption; outputs remain Research-layer until graduated.

**定義（中文）：** 採納前對策略或模型超參數進行系統性網格或區間搜索，以刻畫敏感性；產出屬研究層，採納前不寫入策略 SSOT。

---

### 4.3 Optimal hold (H\*) · 最優持有期 H\*

**Definition (EN):** The holding-period length *H* (in **trading days**) that maximizes a pre-specified objective (e.g. mean excess return vs benchmark, capital-cycle efficiency) within a copytrade or slot backtest grid—not a colloquial “sweet spot” label.

**定義（中文）：** 在跟單或槽位回測網格中，使預先指定目標函數（如相對基準平均超額報酬、資金週轉效率）最大化之持有天數 *H*（**交易日**）——非口語「甜蜜點」標籤。

**Do not use · 勿用:** 甜蜜点 / 甜蜜點 as standalone report terminology.

---

### 4.4 Information coefficient (IC) · 資訊係數

**Definition (EN):** The cross-sectional correlation between a signal (factor) rank and subsequent return rank at a given horizon; **ICIR** is mean IC divided by standard deviation of IC (Grinold & Kahn active-management framework).

**定義（中文）：** 給定持有期下，訊號（因子）排序與後續報酬排序之橫截面相關係數；**ICIR（資訊比率）** 為 IC 均值除以 IC 標準差（Grinold & Kahn 主動管理框架）。

**Lineage:** Grinold & Kahn (1999) *Active Portfolio Management*

---

### 4.5 Walk-forward out-of-sample (OOS) · 滾動樣本外檢驗

**Definition (EN):** Validation scheme where model parameters are estimated on a rolling in-sample window and tested on subsequent unseen data—standard guard against overfitting in factor and strategy research.

**定義（中文）：** 在滾動樣本內窗口估計模型參數、於隨後未見數據上檢驗的驗證方案——因子與策略研究中防止過擬合的標準做法。

---

## 5. Strategy layer terms · 策略層術語

### 5.1 Adopted spec · 採納規格

**Definition (EN):** A frozen strategy entry in `config/strategy.yaml` with `strategies.*.backtest` block (spec type, metrics, JSON path, execution parameters) graduated from Research.

**定義（中文）：** `config/strategy.yaml` 中已凍結之策略條目，含 `strategies.*.backtest` 區塊（規格類型、指標、JSON 路徑、執行參數），由研究層採納 graduation 而來。

---

### 5.2 Copytrade · 跟單交易（持股變動跟隨）

**Definition (EN):** A strategy class that **replicates portfolio-manager actions** inferred from published ETF **holdings changes** (e.g. new positions and size increases on signal day *T*), executing constituent stocks under explicit entry lag *L* and hold *H* rules with PIT constraints.

**定義（中文）：** 依已公布 ETF **持股變動**（如訊號日 *T* 之新进／加码）推斷並**複製投資組合經理行為**的策略類別；在明示進場延遲 *L* 與持有 *H* 規則及時點一致性約束下，於成分股執行。

**Example adopted spec · 採納範例:** `00981a-l1h9` — L1 (T+1 open entry) · H9 (9 trading-day hold) · methodology: `docs/00981a-copytrade-research-methodology.md`

---

### 5.3 Slot strategy backtest · 槽位策略回測

**Definition (EN):** Backtest specification (`spec_type: slot_strategy_backtest`) for strategies with fixed **slot count**, **hold period**, and **capital per signal day**, summarized in slot backtest JSON under `reports/research/`.

**定義（中文）：** 固定**槽位數**、**持有期**與**每訊號日資金**之策略回測規格（`spec_type: slot_strategy_backtest`），摘要見 `reports/research/` 下 slot backtest JSON。

**Contract · 契約:** [evaluation-contract.md](./evaluation-contract.md)

---

### 5.4 Parallel alpha tracks · 並行 Alpha 軌

**Definition (EN):** Multiple adopted strategies run **in parallel** for research and optional screens; **no ensemble weighting** or merged live instruction set.

**定義（中文）：** 多條已採納策略**並行**運行於研究與可選篩選；**無** ensemble 加權或合併實盤指令集。

**Do not use · 勿用:** **Primary track** (legacy six-track framing).

---

## 6. Flow analytics · 籌碼／資金流分析（非 Regime 命名空間）

### 6.1 Flow tape regime · 資金流態勢分類

**Definition (EN):** A **short-horizon, ex-post stratification label** for chip/flow event studies: coarse classification of benchmark (IX0001) 20-trading-day return into bull / bear / range buckets—**distinct** from Regime layer diagnostics and **distinct** from **Trend posture**.

**定義（中文）：** 供籌碼／資金流事件研究用之**短 horizon、事後分層標籤**：將基準（IX0001）20 交易日報酬粗分為多頭／空頭／震盪區間——與**體制診斷層**及**趨勢姿態**均屬不同命名空間。

**Function · 函式:** `flow_tape_regime()` · **Module:** `flow_returns`  
**Do not use · 勿用:** `market_regime()` (deprecated alias).

---

## 7. Deprecated terms · 廢止術語

Before adding new strings, grep this section and [terminology-audit.md](./terminology-audit.md).

| Deprecated · 廢止 | Replace with · 改用 |
|-------------------|---------------------|
| **Evaluation layer** · `track_evaluation` · `evaluation_contract.yaml` · `signal_review` | `config/strategy.yaml` · `strategies.*.backtest` |
| **Exposure overlay** · `exposure_coach_tw` | Removed — see PRD §10 |
| `research_digest` · `research_os` · `p6-tier-flow` | `etf-daily` + `regime-daily` daily close |
| `00981a-v9-hybrid` · `qlib-tw-factor` | `00981a-retired-research.md` · PRD §10 |
| `shared-analytics` · `research-os` | Removed from registry |
| `regime_name` · **Trend regime** | **Trend posture** · `trend_posture` |
| 甜蜜点 / 甜蜜點 (standalone) | **Optimal hold (H\*)** |
| `market_regime` (for index posture) | **Flow tape regime** or **Trend posture** (by context) |
| **Operations layer** (as product name) | **Facts layer** · infra SOP → `daily-operations.md` |
| `RUN_SCORE_ENGINE` · `RUN_RESEARCH_OS` · `RUN_TRACK_EVALUATION` | Retired — see `.env.example` comments |
| `RUN_VCP_FUNNEL` | `RUN_VCP_FUNNEL_SPECS` |

---

## 8. Method lineage · 方法譜系

Cite lineage when introducing methods in reports and docs:

| Tradition · 譜系 | Typical use in this repo · 本專案用途 |
|------------------|----------------------------------------|
| **Weinstein Stage Analysis** · 威斯坦階段分析 | Trend posture · weekly IX0001 stage |
| **Minervini Trend Template / VCP** · 米涅爾維尼趨勢模板／VCP | Stage-2 participation · VCP funnel screen |
| **de Kempenaer RRG** · 相對輪動圖 | RRG rotation quadrant |
| **Zweig / Deemer breadth** · 廣度分析 | Breadth zone · breadth impulse |
| **Grinold & Kahn IC / ICIR** · 資訊係數／資訊比率 | Factor validation · `run_factor_validation.py` |

---

## 9. Related documents · 相關文件

| Document · 文件 | Content · 內容 |
|-----------------|----------------|
| [architecture.md](./architecture.md) | Pipeline · daily close |
| [PRD.md](./PRD.md) | Product scope |
| [evaluation-contract.md](./evaluation-contract.md) | Backtest spec contract |
| [terminology-audit.md](./terminology-audit.md) | Deprecation audit checklist |
