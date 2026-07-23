---
name: tier-a-custom-chip-funnel
description: >-
  Per-sid Tier A customized chip×branch reverse-engineering funnel (P0–P4):
  n-ladder, outcome labels, sequence morphology, optional weight templates,
  OR/AND/score fusion rules, hold-horizon sweep, standalone vs Whale_T+1,
  freeze/OOS and hard-stop. Use for 國巨/2327 custom chip, Tier A chip funnel,
  beat Whale T+1, sequence morphology, whale_chip_precursor per-stock studies.
---

# Tier A 客製籌碼×分點漏斗（逐檔 · Research）

Research only · **未採納**不寫 `config/strategy.yaml` · 不做 Order graduation（除非使用者明確要求）· **MacBook only** · 對齊 `docs/terminology.md`（用**採納**，禁用「畢業」）。

## 準備度判定（本 skill）

| 判定 | 狀態 |
|------|------|
| **Ready（v1 薄漏斗）** | 六支主／輔 runner 已具 `--sid`（預設 `2327`）；非 2327 清 EXTRA／prior，席位來自 `trades/{sid}.json` core |
| **仍非 Ready（全量 P0–P4 × 全 Tier A）** | 見下方「資料窗／擴窗」與「2h 並行預算」— 全相位＋全檔 ≈ 遠超 2h |

流程／勝出門檻／反模式／2327 錨點已齊；**P0 step 0（`--sid`）已清**。下一輪直接挖，但預設窗仍是本機 branch floor。
## README（本 skill）

| 項 | 內容 |
|----|------|
| 目的 | 把 2327 國巨客製化籌碼／分點反推流程，做成**可對任意 Tier A sid 重跑的研究漏斗** |
| 做得到 | 每檔可重複的 P0–P4（+可選 P2.5／H 掃）+ 誠實勝／停／不能打贏產出 |
| **做不到** | 保證每檔都打贏 Whale_T+1（樣本厚薄差很大；2327 greens≈12，有的更薄） |
| 錨點範例 | `reports/research/whale_chip_precursor/2327_*` · `CHIP_WEIGHT_INSPIRATION.md` |
| 宇宙 SSOT | `src/research/chen_chip/whale_events.py` → `TIER_A` |
| 與分點專家漏斗 | **正交**：`branch-specialist-funnel` = 專家池／watch；本 skill = 籌碼×分點**自建進場**挑戰 Whale |

## 可行性（必貼進每檔報告前言）

```text
Doable as a repeatable research funnel per sid.
NOT doable as a promise that every Tier A beats Whale T+1 —
sample sizes vary (2327 greens≈12; some Tier A thinner).
Encode hard stop / "cannot beat" outcomes like the 2327 study.
```

- **做得到**：逐檔可重複漏斗（盤點 → 標籤 → 序列 → 可選權重／融合 → 獨立 BT → H 掃 → 凍結／停）
- **做不到**：承諾 8 檔都能 beat Whale_T+1；n 不夠就必須寫 **cannot beat / stop**

## When to use

- Tier A 客製籌碼／分點反推、挑戰 Whale_T+1
- 國巨同款換到聯電／南亞科／欣興／… 任一 `TIER_A`
- sequence morphology、standalone、OR/AND/score、hold horizon
- 「籌碼 alone 能不能取代 Whale_T+1／專家池跟單」（對照 `CHIP_T0_VS_WHALE_T1`）

### B4 用語（強制分詞 · 禁止混稱「大戶」）

| 寫法 | 指什麼 | 不是 |
|------|--------|------|
| **集保大戶％** | TDCC `HoldingSharesPer` → `big_holder_pct`／`big_holder_pct_chg` | 分點席淨買 |
| **分點席** | broker branch · `securities_trader_id`（例 9268） | 集保週增、Whale 綠燈 |
| **Whale／專家池** | expert-pool 共識 → 外部基準臂 | 集保或單一席的別稱 |

報告／凍結 json／chat：三者分開寫；勿用裸「大戶」同時指集保與分點。

**不要**：只做專家池 P4–P7（用 `branch-specialist-funnel`）；寫進 `strategy.yaml`／launchd（需另開採納任務）。

## Tier A 宇宙 SSOT

```text
src/research/chen_chip/whale_events.py → TIER_A
```

| sid | 常用名 | Whale greens 粗估 |
|-----|--------|-------------------|
| 2327 | 國巨 | ~12（錨點） |
| 2303 | 聯電 | ~13 |
| 3443 | 創意 | **~4（偏薄 · P0 可能硬停）** |
| 6669 | 緯穎 | ~18 |
| 2383 | 台光電 | ~13 |
| 2408 | 南亞科 | ~17 |
| 3189 | 景碩 | ~12 |
| 3037 | 欣興 | ~26（trades 檔可能標其他 tier；**仍以 `TIER_A` 為準**） |

綠燈：`load_whale_events([sid])` → `…/expert_pool/knowledge/trades/{sid}.json`。  
**Whale_T+1 = 外部基準臂 only**。禁止用綠燈子集當挖礦母體（2327 n=4 假進步）。

## 凍結協議（每檔開跑先貼）

| 鍵 | 預設 |
|----|------|
| window | `2024-07-01` → 資料末日（2327 錨點 `2026-07-16`） |
| OOS cut | `2026-01-01` |
| return | **L1H\***：D+1 open → H close · 主對照先 **H=7**，再做 per-sid H 掃 |
| cost / β | 0.003 · **1.15×IX0001** |
| decision D | 收盤判定；籌碼 as-of **D**；分點 **D 與 D−1** |
| overlap | 同股非重疊 |
| source | finmind tape / bars |
| whale | expert-pool greens → **外部對照**，非事件母體 |

改任何鍵 = 新實驗，必須重貼。

### 資料窗／擴窗（必知）

| 層 | 事實 |
|----|------|
| FinMind 分點 API | 約自 **`2021-06-30`** 起有資料（可擴長 IS） |
| 本機 `stocks.db` branch | **floor ≈ `2024-07-01`**（Book replica 現況）；短於此則 tape 空 |
| 預設凍結窗 | 仍用 `2024-07-01` → 資料末日（對齊本機 floor · 與 2327 錨點可比） |
| 要更長 IS | 必須先 **backfill** 目標席／股的 `stock_broker_branch_daily`（FinMind），再改 `--d0`；未 backfill 勿宣稱擴窗結果 |
| 擴窗 probe | `run_2327_multiseat_score_sweep.py --d0 … --bench-only`（腳本內會 clip 到 branch floor） |

**預設漏斗不自動擴窗**；擴窗 = 另開實驗＋重貼凍結協議。計畫見 `reports/research/whale_chip_precursor/TIER_A_DATA_LENGTH_PLAN.md`（W4）。

## 先驗（跨檔共用 · 勿當本檔結論）

| 發現 | 來源 | 含義 |
|------|------|------|
| Chip alone ≪ Whale_T+1 | `CHIP_T0_VS_WHALE_T1.md` | 籌碼黃燈不能取代 Whale／專家池跟單 |
| FROZEN W1–W6 僅前兆 | `FROZEN_PRECURSOR_RULES.md` | Precision 極低；黃燈≠進場 |
| Whale 子集濾網假進步 | `2327_N_DIAGNOSTIC.md` | n=4 = 綠燈∩濾網，不是可交易母體 |
| 序列主路徑 · seat×thr 遞減 | `2327_SEQUENCE_BEAT_WHALE.md` | OOS 可勝但 full mean 未勝 → 停掃，改 exit／regime |
| OR 稀釋 · score 常 wc=0 | `2327_CHIP_OR_SCORE.md` | 勿自由 OR；**B 當閘、C 當 bonus** |
| 民俗權重非 OOS | `CHIP_WEIGHT_INSPIRATION.md` | 0.5/0.3/0.2 folklore；模板 A/B/C 僅靈感 |
| H 非宇宙常數 | `2327_HOLD_HORIZON.md` | 2327 Whale：avg_daily **H=6**、stable **H=3**；每檔自掃 |
| 獨立日曆協議 | `2327_STANDALONE_STRATEGY.md` | D 收盤 → D+1 開 · Whale 外部臂 |

## 腳本復用（優先參數化 · 禁止 8 份 copy-paste）

六支 runner 已支援 `--sid`（預設 `2327`）；非 2327 清國巨 EXTRA／prior，席位／bench seat 來自 `trades/{sid}.json` **core**（2327 仍偏好 `8840` 若在 core）。

| 階段 | 腳本 | 產出前綴 |
|------|------|----------|
| P0 ladder | 可薄包／複用 morphology 日曆計數 | `{sid}_N_DIAGNOSTIC.md` |
| P2–P3 序列 | `run_2327_sequence_morphology_study.py --sid` | `{sid}_SEQUENCE_BEAT_WHALE.md` · `{sid}_seq_*` |
| P3 獨立掃 | `run_2327_standalone_chip_branch.py --sid` | `{sid}_STANDALONE_STRATEGY.md` · `{sid}_standalone_*` |
| P3.5 融合 | `run_2327_chip_branch_or_score.py --sid` | `{sid}_CHIP_OR_SCORE.md` · `{sid}_chip_or_score_*` |
| P3.6 H 掃 | `run_2327_hold_horizon_sweep.py --sid` | `{sid}_HOLD_HORIZON.md` · `{sid}_hold_horizon_*` |
| （輔）多席分數 | `run_2327_multiseat_score_sweep.py --sid` | `{sid}_MULTISEAT_SCORE.md` |
| （輔）A/B/C 模板 | `run_2327_chip_score_templates.py --sid` | `{sid}_CHIP_TEMPLATES_ABC.md` |
| （舊濾網） | `run_2327_chip_branch_t2_t1_sweep.py` · `run_2327_branch_t2t1_topen_sweep.py` | 僅診斷；**主路徑勿綁 Whale 子集** |

（檔名可暫留 `run_2327_*`；行為以 `--sid` 為準。）

## 分相漏斗（鏡像 2327 有效路徑）

```text
P0 inventory / n ladder
  → (n 硬停？寫 cannot-proceed → 結束)
P1 outcome labels（全日曆 forward excess · 非 Whale 子集）
P2 sequence morphology mine（主路徑）
P2.5 optional weight templates A/B/C（靈感 · 非 OOS 權重）
P3 standalone calendar BT vs Whale_T+1
P3.5 OR/AND/score（規則見下 · 非自由 OR）
P3.6 hold-horizon sweep（per-sid · H≠宇宙常數）
P4 freeze + OOS · win / stop / cannot-beat
  → (seat×threshold 遞減？切 exit／regime，不無限掃)
```

每檔檢查清單：

```text
- [ ] 貼凍結協議 + 可行性聲明 + 資料窗（本機 floor vs 擴窗）
- [ ] `--sid` 正確；core 來自 trades/{sid}.json（非 2327 EXTRA／prior）
- [ ] P0：n ladder → {sid}_N_DIAGNOSTIC.md
- [ ] P0 硬停？→ 停並記錄
- [ ] P1：outcome labels（非 greens）
- [ ] P2：序列挖礦 · 禁止 Whale 子集
- [ ] P2.5（可選）：權重模板 · 標 folklore／勿同窗擬合 w
- [ ] P3：獨立日曆 BT · Whale 外部對照
- [ ] P3.5：融合時 B=gate、C=bonus；記錄 OR 稀釋
- [ ] P3.6：H 掃（Whale + 凍結臂）寫入 {sid}_HOLD_HORIZON.md
- [ ] P4：凍結或 cannot-beat / hard-stop 明文
- [ ] 未寫 strategy.yaml · 未動 mini launchd
```

### P0 — Inventory / n ladder

寫 `reports/research/whale_chip_precursor/{sid}_N_DIAGNOSTIC.md`：

| definition | n |
|------------|--:|
| calendar trading days | |
| domestic branch 任一日 net>0 / ≥0.5億 / ≥1億 | |
| champion seats ≥0.5億（core） | |
| chip fire（例 ts1+bhchg / ts≥2）日曆 | |
| chip ∧ champ 同日 | |
| **Whale greens（external only）** | |
| standalone 非重疊 H7 粗估（chip-only / chip∧branch） | |

**硬停（P0）**：

| 條件 | 動作 |
|------|------|
| Whale greens **n &lt; 5** | 禁止宣稱 beat Whale；可做 `NO_WHALE_BENCHMARK` 日曆探索 |
| 日曆可觀測火非重疊 H7 粗估 **n &lt; 8** | 樣本不夠凍結 |
| `trades/{sid}.json` 缺失或 core 空 | 先補 expert-pool knowledge |

3443 等 greens≈4：預設 `cannot_proceed vs Whale`。

**教訓（2327）**：永遠不要把「綠燈 ∩ 最佳濾網」的 n（如 n=4）當可交易母體評估。

### P1 — Outcome labels（非 Whale-subset）

- 母體 = 窗內可算 L1H7 的交易日 D
- 標籤例：`good_gt0` / `good_gt3` / `good_gt5` / `good_medp`
- **禁止**只在 greens 上挖 lift

### P2 — Sequence morphology（主路徑）

對齊 `run_2327_sequence_morphology_study.py`（參數化後）：

- seat continuity / intensity / accel / rank
- multi-seat co-buy · stagger（D−1→D）
- chip path · chip×seat
- 可選 3-day（iter2+）；**IS-only lift**；`MIN_LIFT_N_GOOD` 守門

假勝出過濾：OOS 好看但 **IS mean &lt; 0** → 不算 win（2327 `seat_8840__accel`）。

2327 結局模板：可達 **OOS 門檻**但 **full mean 未勝 Whale** → 記 `cannot_beat_full_mean`；**停止** seat×threshold／pXX 網格；下一槓桿 = **出場／regime／改目標（sum）**。

### P2.5 — Optional weight templates（靈感 · 非必做）

來源：`CHIP_WEIGHT_INSPIRATION.md`。皆 **Research 模板**；**勿當 OOS 估計權重**。

| 模板 | 做法 | 何時試 |
|------|------|--------|
| **B** 紅黃綠桶 | W1–W6 條件 −1/0/+1 **等權加總**；`score_B≥2`（IS 可掃 2/3）+ margin 否決 | **優先**（可解釋、難過擬合） |
| **C** rank 融合 | 60d pct_rank 等權（可選固定 lift tilt，**不掃 w**） | B 有形狀後並行 |
| **A** 線性 z-score | 60d rolling z × 固定先驗（例 foreign 0.30…）；民俗 0.5/0.3/0.2 **勿當真理** | 僅 B/C 穩定後；**禁止同窗迴歸擬合 w 再報 OOS** |

落地順序：B → C →（可選）A 固定 tilt。分點仍當**主導或硬閘**；籌碼分數 = 黃燈／加分，不可單獨開倉。

### P3 — Standalone calendar BT vs Whale_T+1

協議（`2327_STANDALONE_STRATEGY.md`）：

- D 收盤：籌碼@D + 分點@D/D−1 → **D+1 開** · 非重疊
- Whale_T+1 外部臂（同 H/cost/β · **不同母體** · 非配對 bootstrap）
- 主指標：**mean excess**；sum 輔助；hit 不單獨勝出
- 產出：`{sid}_STANDALONE_STRATEGY.md`

### P3.5 — OR / AND / score（規則 of thumb）

來源：`2327_CHIP_OR_SCORE.md`。

| 融合 | 2327 教訓 | 漏斗規則 |
|------|-----------|----------|
| **OR**（B∨C） | 多成交、**稀釋 mean** | **禁止**當主結論；若報必須拆 OR 增量 mean |
| **AND**（B∧C） | 高 mean 但 **極稀疏** | 可記；n 不夠不凍結 |
| **Score** wb/wc/… | IS 最佳常 **wc=0**（沒用 C） | 稀疏＋分點主導；含 C 常不贏 B |
| 權重混加 | branch+chip  naive 權重 | **勿**把 B 與 C 丟進同一 naive weight 當「融合勝利」 |

**偏好**：**B as gate，C as bonus**（C 只能收緊／加分，不能自由 OR 放寬母體）。

### P3.6 — Hold-horizon sweep（per-sid）

來源：`2327_HOLD_HORIZON.md`。

- 對 **Whale_T+1** 與凍結序列臂掃 H∈{1..15}（或專案預設格）
- 報：`avg_daily = mean/H` · `stability_score`（見該報告公式）
- **2327 錨點**：Whale avg_daily 冠軍 **H=6**；stable **H=3**；經典 H=7 **非最優**
- **H 不是宇宙常數** → 每檔必須自掃；不可把 2327 的 H=6/3 抄到他股當凍結

產出：`{sid}_HOLD_HORIZON.md`。主對照表仍可用 H=7 與錨點可比；凍結時可另標 `H*`。

### P4 — Freeze · OOS · win / stop

#### 勝出（至少一條）

| # | 條件 |
|---|------|
| 1 | OOS mean ≥ Whale OOS **且** OOS n≥5 **且** IS n≥8 **且** IS mean≥0 |
| 2 | Full mean ≥ Whale full **且** bootstrap P(Δ≤0)≤0.15 **且** n≥12 **且** IS mean≥0 |
| 3 | Full sum 明顯更高（>+10%）**且** mean 不差過 2pp（n≥12 · IS mean≥0） |

達標 → `{sid}_seq_frozen.json` / `{sid}_standalone_frozen.json`。  
**達標 ≠ 採納**。

#### Hard stop / cannot beat

| 情境 | 狀態 | 下一步 |
|------|------|--------|
| 掃完序列家族無勝出 | `cannot_beat_whale` | 結束；改目標需新協議 |
| OOS 微勝但 full mean 輸，再緊門檻踩 IS n | `cannot_beat_full_mean`（2327） | **停** seat×threshold／pXX |
| Top 規則 IS n&lt;8 或靠 OOS 1–2 肥尾 | `unstable_oos` | 不凍結 |
| Chip-only / 平坦 branch 全面輸 | `chip_or_flat_branch_insufficient` | 進序列；仍輸則 cannot_beat |

#### 何時停 seat×threshold · 改 exit／regime

同時成立即停掃：

1. ≥2 輪序列／combo 無 full-mean 勝出  
2. 再緊 pXX／億級門檻只逼近 Whale 但踩 `IS n≥8`  
3. 已能解釋為何 full mean 未贏  

允許下一研究（新協議）：出場／追蹤停利、regime 分層、目標改 sum／容量。

## 準備度檢查清單

### 已就緒（流程／知識）

| 項 | 狀態 |
|----|------|
| P0–P4 漏斗 + 勝出／hard-stop | ✅ |
| 序列 = 主路徑；Whale 子集禁止 | ✅（2327 驗證） |
| OR/AND/score 經驗法則 | ✅ |
| 權重模板 A/B/C 靈感（非 OOS w） | ✅ `CHIP_WEIGHT_INSPIRATION.md` |
| Hold-horizon per-sid 步驟 | ✅ |
| 獨立日曆協議 | ✅ |
| 反模式 + MacBook／未採納邊界 | ✅ |
| 錨點報告完整 | ✅ `2327_*` |
| runners `--sid` + core seats | ✅ 六支（sequence／standalone／or_score／hold／multiseat／templates） |
| 資料窗註記（FinMind≈2021-06 · DB floor 2024-07） | ✅ |

### 阻擋項（開挖前仍須守）

| 阻擋 | 動作 |
|------|------|
| 未寫 n ladder 就開挖 | 先 `{sid}_N_DIAGNOSTIC.md` |
| 承諾每檔 beat Whale | 禁止；薄樣本走 cannot_proceed |
| 擴窗未 backfill | 勿改 `--d0` 早於本機 floor 卻當完整 IS |

### 可選／非阻擋

| 項 | 說明 |
|----|------|
| P2.5 權重模板 runner | 2h v1 **跳過**；有空再跑 |
| P3.5 全 OR grid | 2h v1 **跳過**；僅記 B-gate／C-bonus 若有時間 |
| 檔名仍叫 `run_2327_*` | 可接受，只要 `--sid` 正確 |
| H* 寫進凍結 json | 建議；主表仍可留 H=7 對照 |
| FinMind 擴窗 backfill | 另開實驗，非本輪阻擋 |

## 反模式（Anti-patterns）

| 反模式 | 為何錯 |
|--------|--------|
| Whale 子集 n=4（綠燈∩濾網）當主結論 | `2327_N_DIAGNOSTIC`：不是可交易母體 |
| 自由 **OR chip** 放寬進場 | `2327_CHIP_OR_SCORE`：稀釋 mean |
| 同窗擬合權重 w 再報 OOS | folklore／多重檢驗；權重先驗或 nested |
| branch+chip **naive 混權**當融合勝利 | score 最佳常 wc=0；應用 B-gate／C-bonus |
| 承諾每檔打贏 Whale | 樣本／日曆結構不同；漏斗允許 cannot_beat |
| 把 2327 的 H=6/3 或 8840 席抄到他股 | H 與席位皆 **per-sid** |
| 無限 seat×threshold／pXX 掃參 | 2327 已遞減；改 exit／regime |
| 複製 8 份 `run_{sid}_*.py` | 違反復用 |
| 無勝出仍凍結／暗示 Order | 越權 |
| 寫入 `strategy.yaml`／mini launchd | 違反邊界 |

## 交付路徑

根目錄：`reports/research/whale_chip_precursor/`

| 產物 | 路徑 |
|------|------|
| n 階梯 | `{sid}_N_DIAGNOSTIC.md` |
| 獨立策略 | `{sid}_STANDALONE_STRATEGY.md` |
| 序列挑戰 | `{sid}_SEQUENCE_BEAT_WHALE.md` |
| OR/score | `{sid}_CHIP_OR_SCORE.md` |
| H 掃 | `{sid}_HOLD_HORIZON.md` |
| 凍結 json | `{sid}_seq_frozen.json` · `{sid}_standalone_frozen.json` · `{sid}_chip_or_score_frozen.json` |
| CSV／panel | `{sid}_seq_*` · `{sid}_standalone_*` · `{sid}_chip_or_score_*` · `{sid}_hold_horizon_*` |
| 狀態 | `status_{sid}_*.json` |
| 跨檔唯讀 | `CHIP_T0_VS_WHALE_T1.md` · `FROZEN_PRECURSOR_RULES.md` · `CHIP_WEIGHT_INSPIRATION.md` · `BRIEFING.md` |

## 成功／失敗判準

### 成功（漏斗成功 · 非市場保證）

| 結果 | 判定 |
|------|------|
| 完整 P0–P4 + 誠實表 + 凍結或 cannot-beat | **漏斗成功** |
| 達勝出且 IS mean≥0 | **研究凍結候選**（仍未採納） |
| P0 硬停並寫清 n 不夠 | **漏斗成功（負結果）** |

### 失敗（代理人／流程失敗）

見上「反模式」表；任一發生 = 流程失敗。

## 並行與機器

| 項 | 規則 |
|----|------|
| 機器 | **MacBook** 研究；不 SSH mini、不裝 live launchd |
| 並行 | 同輪 ≤3 sid；同 sid 腳本**序列**（P2→P3→薄 H） |
| DB | Book replica `data/stocks.db`（或專案預設 RO）· branch floor `2024-07-01` |

### 2h 並行預算（v1 薄漏斗）

**目標**：7 檔待跑（跳過已厚做的 2327）× **P0 n-ladder + P2 sequence + P3 standalone vs Whale**；不承諾全相位。

| 波次 | sids（≤3） | 相位 | 估時 |
|------|------------|------|------|
| 0 | 全檔薄 | P0 n-ladder（可並寫）· 3443 預標 `cannot_proceed vs Whale` | ~10–15m |
| 1 | **3037 · 6669 · 2408** | P2 sequence → P3 standalone | ~45–70m |
| 2 | **2303 · 2383 · 3189** | 同上 | ~45–70m |
| 3（可選） | 3443 | 僅日曆探索／或跳過 | 剩時 |

**2h 內跳過**：P2.5 全 A/B/C 模板、P3.5 全 OR／score grid、完整 P3.6 H∈{1..15}（可改 `--skip-secondary` 或只掃 H∈{3,6,7,10}）、FinMind 擴窗 backfill、2327 重跑。

**誠實判定**：**2h 不夠**跑完「全 Tier A × 全 P0–P4」；**夠**做 v1 薄漏斗（P0+P2+P3）於厚樣本前 3–6 檔（兩波並行）。

## 禁止

- 承諾「Tier A 全能打贏 Whale」
- Whale-subset 當挖礦母體
- Chip alone／自由 OR chip 當進場主策略
- 同窗擬合 w 當 OOS 權重
- 未參數化就把 2327 EXTRA／prior 席套到他股
- 採納進 Strategy／Order（除非使用者明確另開任務）
- Canvas 當主交付（本倉庫偏好 chat）

## 本庫錨點（2327 已跑通）

| 用途 | 路徑 |
|------|------|
| n 診斷 | `…/2327_N_DIAGNOSTIC.md` |
| 獨立策略 | `…/2327_STANDALONE_STRATEGY.md` |
| 序列勝出／停 | `…/2327_SEQUENCE_BEAT_WHALE.md` |
| OR/score | `…/2327_CHIP_OR_SCORE.md` |
| Hold H* | `…/2327_HOLD_HORIZON.md` |
| 權重靈感 | `…/CHIP_WEIGHT_INSPIRATION.md` |
| Chip vs Whale | `…/CHIP_T0_VS_WHALE_T1.md` |
| 前兆凍結 | `…/FROZEN_PRECURSOR_RULES.md` |
| TIER_A | `src/research/chen_chip/whale_events.py` |
| runners | `scripts/research/run_2327_{sequence_morphology_study,standalone_chip_branch,chip_branch_or_score,hold_horizon_sweep}.py` |

## 8 檔／下一檔怎麼跑（操作序）

1. 讀 `TIER_A` · greens n（現況：3037=26 · 6669=18 · 2408=17 · 2303/2383=13 · 2327/3189=12 · **3443=4**）  
2. 建議序：厚樣本先（3037→6669→2408→2303/2383→3189）· **跳過重跑 2327** · 薄樣本最後（3443）  
3. **每檔**：`--sid S` → P0 ladder → 硬停？→ P2 sequence → P3 standalone →（時間夠再 P3.5／薄 H）→ P4  
4. 彙整（chat）：sid · greens n · 凍結或 `cannot_beat` · Δmean vs Whale · H*  
5. **不要**批次自動採納

### v1 下一指令（例 · 波次 1 單檔）

```bash
PYTHONPATH=src .venv/bin/python scripts/research/run_2327_sequence_morphology_study.py --sid 3037
PYTHONPATH=src .venv/bin/python scripts/research/run_2327_standalone_chip_branch.py --sid 3037
PYTHONPATH=src .venv/bin/python scripts/research/run_2327_hold_horizon_sweep.py --sid 3037 --skip-secondary
```

並行：開 ≤3 個 terminal／agent，各綁一 sid；同 sid 內保持上列順序。

---

**Readiness verdict：Ready（v1 薄漏斗）** — `--sid` 已清；預設本機窗 `2024-07-01+`。全相位×全檔或 FinMind 擴窗另計。
