# PRD · Order layer（下單層）完整藍圖

| 欄位 | 內容 |
|------|------|
| 版本 | 1.2 |
| 日期 | 2026-07-16 |
| 狀態 | **Living doc** — 以 `config/order.yaml` · `src/order/` 為準 |
| 上層 PRD | [PRD.md](./PRD.md) §1 下單層 |
| 術語 SSOT | [terminology.md](./terminology.md) §2.5 |
| 架構 | [architecture.md](./architecture.md) · [src-map.md](./src-map.md) §下單層 |
| 盤中減碼 | [intraday-exit-playbook.md](./intraday-exit-playbook.md) |
| Agent 導航 | [agent-brief.md](./agent-brief.md) |
| 現況／live | [config/job_registry.yaml](../config/job_registry.yaml) |

> **免責**：本文件描述本機 infra 與個人研究執行框架；**不**構成投資建議。Order layer **不**進公開網站、**不**暴露券商憑證。
>
> **2026-07-29 現況**（SSOT：[config/job_registry.yaml](../config/job_registry.yaml)）：**全部 order-capable job（C18acc／Leading Dip／Songshan copytrade／expert-pool staged gate／detach-gate）已暫停**（`.env` flag safe + `launchctl disable` + `ORDER_MASTER_ENABLED=0` 三重保險），目前**無一支**實際具下單能力；`timed-limit-orders` 已於 2026-07-26 整支移除（launchd + 程式碼 + config），之後不再使用。  
> **ABC Order 已退役**（送單入口硬擋；`ABC_V3_F1_ORDER_ENABLED=0`）。下文含歷史 Phase／ABC 段落時以本框為準。

---

## 1. 摘要

Order layer（下單層）是 **Strategy layer（策略層）與富邦 Neo API 之間的本機執行基礎設施**：將 radar / screen 產出的訊號轉為 `order-intent-v1` JSON，經閘門與 re-entry 政策後送單，並以 ledger、log、email 留痕。

**設計原則（避免技術債）**

| 原則 | 說明 |
|------|------|
| **邊界單向** | Strategy **不** import `order`；接線只在 `scripts/`（如 `run_buy_signal_radar.py`） |
| **本機 SSOT** | `config/order.yaml` + `.env`；無第三方 OMS SaaS、無額外註冊 |
| **不 fork 外掛 stack** | 不整包引入 OpenAlgo / Hummingbot；僅借 IA 與 API 契約概念 |
| **最小 live surface** | 優先唯讀 ops snapshot；送單路徑保持既有 Python 模組 |
| **PIT 與 research 對齊** | Re-entry、hold、notional cap 對齊已採納 research event 口径 |

---

## 2. 問題陳述

### 2.1 現況痛點（2026-07-09）→ 緩解狀態（2026-07-13）

| # | 痛點 | 緩解 |
|---|------|------|
| P1 | `place_order` 成功 ≠ 成交 | `lifecycle.notional_basis: filled`；現金／曝險僅 filled/partial；poll 無列 → `ambiguous` + notional 0 |
| P2 | 通知綁 observe 首次命中 | 既有 submit_notify 解耦（Phase 1） |
| P3 | 無委託狀態機 | 短 poll + 每輪 `refresh_open_ledger_entries`；仍非完整 async OMS |
| P4 | 無 broker reconciliation | `scripts/order/reconcile_broker_ledgers.py` **報告 only**（不自動改帳） |
| P5 | 無 ops 後台 | `scripts/order/write_ops_snapshot.py` → `reports/order/snapshots/*_ops.json` |
| P6 | intent 失敗仍留檔 | 失敗 → `intents/quarantine/`；`submit_intents.py --submit` 拒絕已標記／placeholder |

### 2.2 非問題（刻意不做）

- 多券商 unified API（僅富邦 Neo）
- 公開網站下單
- Ensemble 加權合併多策略成一筆委託
- 取代富邦官方 App 的完整 OMS

---

## 3. 目標與非目標

### 3.1 產品目標

1. **可審計**：每筆 live 意圖有 intent JSON + ledger + log 可追溯。
2. **可恢復**：網路 / API  ambiguous 失敗時，下一 poll 可安全重試、不重複下單。
3. **可觀測**：本機單頁（snapshot 或 HTML）看今日 abc 委託、launchd 健康、開關狀態。
4. **對齊 research**：abc-v3-f1 sleeve 的 hold 3d、re-entry 折扣、rolling cap 與採納規格一致。
5. **零訂閱**：僅富邦 API（既有帳戶）+ 本機 Gmail notify；ops UI 不依赖 Supabase 亦可運作。

### 3.2 非目標（Out of Scope）

| 項目 | 說明 |
|------|------|
| Fork OpenAlgo / Hummingbot | AGPL / Docker 債務過大 |
| 常駐 Flask + Socket.IO 服務 | 除非 semi-auto 核准有明確需求 |
| Phase 3 前自動賣出 abc T+3 | 用戶明確排除；C18 / structural exit 另軌 |
| 公開 Readdy `/ops` 含 live 帳務 | 憑證僅本機 |

### 3.3 成功指標（KPI）

| 指標 | Phase 1 目標 | Phase 2 目標 |
|------|--------------|--------------|
| 送單 duplicate rate | 0（同日同 symbol 誤重送） | 0 |
| ambiguous 失敗後 15 分鐘內收斂 | ≥90% 有明確 terminal 狀態 | ≥99% |
| submit 成功通知覆蓋 | 100%（含重試成功） | 100% |
| ops snapshot 新鮮度 | — | 每 radar poll 或 ≤5 分 |
| 單元測試 | abc re-entry + order gates 全綠 | + lifecycle + snapshot |

---

## 4. 使用者與場景

| 角色 | 場景 |
|------|------|
| **Operator（操作者）** | 盤中收到 observe / 送單 email；本機打開 ops 頁確認委託 |
| **Developer** | 新增 sleeve 時只擴 `order.yaml` + intent bridge，不動 strategy import 鏈 |
| **Research** | 讀 ledger 對照 backtest event，驗證 live 口径 |

**典型日內流程（abc-v3-f1）**

```mermaid
sequenceDiagram
  participant L as launchd buy-signal-radar
  participant S as strategy_signal_radar
  participant B as abc_v3_f1_intent_bridge
  participant O as order/abc_v3_f1_order
  participant F as Fubon Neo API
  participant E as email notify

  L->>S: run_buy_signal_radar
  S->>S: pool observe hits
  S->>E: new_signals → 寄信（首次）
  L->>O: process_abc_v3_f1_orders(signals)
  O->>O: re-entry gates + ledger
  O->>B: write intent JSON
  O->>F: chase_ask place_order
  F-->>O: is_success / order_no
  O->>O: ledger append（成功時）
  L->>L: print log（含 已送單/略過）
```

---

## 5. 系統邊界

### 5.1 產品層位置

```
facts → regime → research → strategy (+ order layer 本機)
                              ↓
                    order-intent-v1 JSON
                              ↓
                         src/order/ → Fubon
```

### 5.2 Import 規則（硬邊界）

| 允許 | 禁止 |
|------|------|
| `scripts/*` import `order.*` | `src/strategy_*.py` import `order.fubon_*`（新程式） |
| `strategy_signal_radar` import `abc_v3_f1_intent_bridge`（log 格式化） | Strategy 模組直接 `place_order` |
| Research / backtest 寫 intent JSON | Research import live broker |

### 5.3 設定 SSOT

| 檔案 | 用途 |
|------|------|
| `config/order.yaml` | broker、intent schema、strategy sleeve 參數 |
| `.env` / `.env.example` | 憑證、`ORDER_*` / `C18ACC_*` / `RUN_*`（ABC `ABC_V3_F1_*` 僅退役殘留＝0） |
| `config/buy_observation.yaml` | observe pool（Strategy 側；order 只讀 pool_id） |

---

## 6. 現況盤點（As-Is）

### 6.1 模組地圖

| 路徑 | 職責 | 成熟度 |
|------|------|--------|
| `src/order/intent.py` | `order-intent-v1` 契約 | 穩定 |
| `src/order/fubon_session.py` | 登入 · SDK 版本 | 穩定 |
| `src/order/fubon_orders.py` | `place_resolved_order` | 穩定 · 缺 lifecycle |
| `src/order/chase.py` · `chase_runner.py` | 追價撤單重掛 | 穩定 · C18/chase 用 |
| `src/order/abc_v3_f1_*.py` | ABC 自動送單（**retired 2026-07-16** · FORCE_LEGACY only） | 退役 · 勿當 live |
| `src/order/c18acc_order*.py` | C18acc 進／換／出場送單 | live |
| `src/order/c18acc_position_monitor.py` | C18acc underwater_rebound pyramid add（O3-7） | 接入 rrg poll · **採納開啟** |
| `src/order/leading_dip_*.py` | Leading Dip 衛星袖 | live |
| `src/order/detach_gate_order.py` | Detach Gate 全帳戶 half-flatten | 排程在 · **RED 只寄信**（`ORDER_ENABLED=0` · 不送單） |
| `src/abc_v3_f1_intent_bridge.py` | Strategy-safe intent 寫入（legacy） | 退役路徑 |
| `scripts/order/submit_intents.py` | 人工 intent 送單 CLI | 穩定 |
| `scripts/run_buy_signal_radar.py` | 買訊觀測／寄信 · **不送單** | live advisory |
| `src/order/intraday_exit_gate.py` | 09:05 組合閘門 | **Research only**（已撤 launchd） |
| `src/order/intraday_structural_exit.py` | 結構停損掃描 | **Research only**（sell radar 不再呼叫） |
| `src/order/holdings_pulse.py` | 持倉 HTML 脈動 | 報告 · 非 OMS |
| `src/order/morning_holdings_brief.py` | 晨間 brief | 報告 |

### 6.2 Live 送單 sleeve（2026-07-16）

**ABC Order 已退役**：`config/strategy.yaml` · `abc-v3-f1-*` 為 `enabled: false`；`order.yaml` 無 ABC sleeve；送單入口需 `ABC_V3_F1_ORDER_FORCE_LEGACY=1`（僅庫測）。buy-signal-radar **不送單**；ABC 觀測池 `buy_observation.yaml` 亦 `enabled: false`。

| Sleeve | 觸發 | 自動送單 | 上限／備註 |
|--------|------|----------|------------|
| **C18acc / rrg-mono-swap-accel** | ~~`rrg-c18acc-poll`~~ **排程 2026-08-04 退役** | ❌ 無排程可觸發（registry `enabled: false` + `.env` 旗標 0） | 程式碼／`order.yaml` 規格未動；手動 `scripts/run_rrg_mono_swap_accel_screen.py` |
| **Leading Dip**（+ mid） | `leading-dip-poll` | ✅（`ORDER_LEADING_DIP_*`） | coverage + mid · 與 C18 互斥 |
| **Detach Gate** | `detach-gate` | ❌ 已暫停（`launchctl disable` + `ORDER_ENABLED=0`） | 半砍規格保留（floor(qty/2) @ bid1）· 現況不送單 · 見 config/job_registry.yaml |
| **buy / sell signal radar** | launchd | ❌ 寄信 only | ABC 池關閉 |
| **chase_open** | `schedule.enabled: false` | — | 已停用 |

C18acc 漏斗（Strategy／Order 對齊）：**fresh mono** 全池 · 池序 `seg_last` · 進場 `avg_accel_decel` · **confirm_bars=1** · 換倉 `avg_accel_decel`（門檻 `seg_last+0.05`）· E@13:00 · avoid spread_mixed · G_R5_12 · **pyramid underwater_rebound 採納**。詳 `config/strategy.yaml` · `config/order.yaml`。

### 6.3 資料與產物

| 路徑 | 內容 |
|------|------|
| `reports/order/intents/` | intent JSON（審計） |
| `data/order/abc_v3_f1_ledger.json` | abc 送單 ledger |
| `logs/intraday/launchd_buy-signal-radar.log` | radar + 下單 log |
| `reports/order/snapshots/` | 帳戶 snapshot（若腳本寫入） |

### 6.4 已完成的近期修復

- abc 下單改吃 **當 poll `signals`**（非 dedup `new_signals`），送單失敗可重試。
- `abc_notional_for_symbol` 改 **rolling 20TD** 窗口。

---

## 7. 目標架構（To-Be）

### 7.1 三層執行模型（借 OpenAlgo 概念 · 自實作）

```
┌─────────────────────────────────────────────────────────┐
│  Strategy / Scripts（訊號 · observe · intent 作者）        │
└───────────────────────────┬─────────────────────────────┘
                            │ order-intent-v1 + metadata
┌───────────────────────────▼─────────────────────────────┐
│  Order Policy（閘門 · re-entry · budget · kill switch）   │
│  abc_v3_f1_reentry · config/order.yaml · env             │
└───────────────────────────┬─────────────────────────────┘
                            │ ResolvedOrder + client_intent_id
┌───────────────────────────▼─────────────────────────────┐
│  Broker Adapter（富邦 Neo）                               │
│  fubon_session · fubon_orders · chase_runner             │
│  + OrderLifecycle（poll · reconcile · terminal state）    │
└───────────────────────────┬─────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────┐
│  Audit & Ops（ledger · DB/json state · snapshot · notify）│
└─────────────────────────────────────────────────────────┘
```

### 7.2 委託狀態機（新增 · 核心）

| 狀態 | 說明 | 來源 |
|------|------|------|
| `intent_written` | intent JSON 已寫 | bridge |
| `submitted` | Fubon `is_success` | place_order |
| `working` | 委託中（status 10） | get_order_results |
| `partial` | 部分成交 | filled_qty |
| `filled` | 完全成交（status 50） | get_order_results |
| `cancelled` | 已刪除（30） | get_order_results |
| `failed` | 明確拒絕 / 90 | API message |
| `ambiguous` | 超時未確認 | 需 reconcile |

**規則**

- ledger **filled 才**計入 notional 曝險（或 configurable：submitted vs filled）。
- `ambiguous` → 以 deterministic `client_intent_id` 查單後再決定 retry。

### 7.3 Deterministic intent ID（冪等）

格式建議：

```
{strategy_id}_{session_date}_{poll}_{symbol}
```

- 寫入 intent `metadata.client_intent_id` 與 Fubon `user_def`（或 metadata 檔）。
- 重試 **同一 ID**；禁止每次 new UUID。

### 7.4 Ops snapshot 契約（`order-ops-snapshot-v1`）

**目的**：本機唯讀後台 · 零常駐 server · 可選餵給 readdy `/ops`。

```json
{
  "schema_version": "order-ops-snapshot-v1",
  "as_of": "2026-07-09T13:05:00+08:00",
  "session_date": "2026-07-09",
  "kill_switches": {
    "abc_v3_f1_order_enabled": true,
    "abc_v3_f1_auto_submit": true
  },
  "abc_v3_f1": {
    "daily_entries": 2,
    "daily_budget_used_twd": 40000,
    "ledger_entries_today": [],
    "last_poll_orders": []
  },
  "launchd": {
    "buy_signal_radar_last_tick": "…",
    "jobs_ok": []
  },
  "broker": {
    "connected": false,
    "open_orders_n": null,
    "note": "optional · 需本機 Fubon session"
  }
}
```

**產出**：`reports/order/snapshots/{YYYYMMDD}_ops.json` + 可選靜態 HTML（參考 `holdings_pulse` 模式）。

**UI 路線（推薦）**

1. Python 排程產 snapshot（無新 dependency）
2. 本機開 HTML 或 readdy 唯讀頁（不暴露憑證）
3. **不**引入 Streamlit，除非 Phase 2 驗證後仍缺 UI 再評估

### 7.5 通知解耦

| 事件 | 通道 | 觸發 |
|------|------|------|
| observe 首次命中 | email（現行） | `new_signals` |
| submit 成功 | email **獨立段落或獨立信** | ledger 新增 submitted |
| submit 失敗 | email 或 log 高亮 | status failed / ambiguous |
| filled（可選） | email | lifecycle → filled |

---

## 8. 功能需求（按 Phase）

### Phase 0 · 現行（已完成 baseline）

- [x] `order-intent-v1` + `submit_intents.py`
- [x] abc-v3-f1 auto-submit + re-entry policy
- [x] radar 接線 `run_buy_signal_radar.py`
- [x] P0/P1：signals 重試 + rolling notional
- [x] 單元測試 `tests/test_abc_v3_f1_order.py`

### Phase 1 · Mini-OMS（委託生命週期 · 優先）✅ 2026-07-09

| ID | 需求 | 驗收 |
|----|------|------|
| O1-1 | `client_intent_id` 貫穿 intent / user_def | ✅ |
| O1-2 | submit 後 poll `get_order_results` 更新狀態 | ✅ |
| O1-3 | ambiguous 超時 → 查單再決定 | ✅ `reconcile_before_submit` |
| O1-4 | failed submit 清理或標記 intent | ✅ `submit_status` metadata |
| O1-5 | submit 成功通知（含重試） | ✅ `ABC_V3_F1_SUBMIT=1` |
| O1-6 | notional 以 filled 或 submitted 可配置 | ✅ `lifecycle.notional_basis` |

**不改**：Strategy import 邊界 · 富邦 SDK 版本 · abc re-entry 參數預設。

### Phase 2 · Ops 可觀測（唯讀 · 零訂閱）

| ID | 需求 | 驗收 |
|----|------|------|
| O2-1 | `build_order_ops_snapshot()` | JSON schema v1 穩定 |
| O2-2 | radar 結束或獨立 launchd 寫 snapshot | 檔案時間戳 ≤5 分 |
| O2-3 | 靜態 HTML ops 板（5 屏） | Orders · Ledger · Launchd · Switches · Log tail |
| O2-4 | 整合 `launchd_status_dashboard.sh` 摘要 | 同一 snapshot |
| O2-5 | （可選）readdy `/ops` 讀 snapshot | **不**含 secrets |

**參考 IA（OpenAlgo 五屏 · 不抄 code）**

1. Today orders  
2. Positions / holdings（唯讀 Fubon 或 morning brief）  
3. Kill switches / env  
4. Launchd health  
5. Log tail  

### Phase 3 · 進階（按需 · 非承諾）

| ID | 需求 | 條件 |
|----|------|------|
| O3-1 | abc T+3 13:30 自動出場 | 用戶明確批准 + backtest 對齊 |
| O3-2 | Semi-auto Action Center | 送單前人工核准 UI |
| O3-3 | 週期 broker reconciliation job | 比對 inventory vs ledger |
| O3-4 | C18acc poll 自動送單 | 獨立 sleeve 規格 + 風控 |
| O3-5 | ABC v3+F1 champion TP 持倉 poll | **retired 2026-07-16** |
| O3-6 | ABC v3+F1 underwater_rebound pyramid add | **retired 2026-07-16** |
| O3-7 | C18acc underwater_rebound pyramid add | **採納** · `c18acc_pyramid_add.enabled:true` · rrg poll 內掃 |

### Phase 4 · 明確不做

- Fork OpenAlgo / Hummingbot 後端  
- 多券商 adapter  
- 公開網站 live 下單  
- 替代富邦 App 的全功能 OMS  

---

## 9. 非功能需求

| 類別 | 要求 |
|------|------|
| **安全** | 憑證僅 `.env`；snapshot / HTML **不含** password / cert |
| **可用性** | launchd 下單失敗不 crash radar（現行 `try/except` 保留） |
| **效能** | 單 poll abc hits ≤10 檔；Fubon 連線單 session 串行 |
| **可測** | 新邏輯必須 mock Fubon；不依 live API 跑 CI |
| **可維護** | 新 sleeve = config block + `{sleeve}_order.py` + tests |
| **授權** | 不 copy AGPL 程式碼進 repo；僅參考文件與 IA |
| **成本** | $0 新增 SaaS；富邦 API 沿用既有帳戶 |

---

## 10. 設定與環境變數

### 10.1 `config/order.yaml`（現行 + 規劃）

```yaml
# 現行 live（2026-07-16）
strategies:
  rrg-mono-swap-accel:
    entry_confirm_bars: 1
    budget_twd_per_slot: 20000
  leading-dip:
    budget_twd: 20000

lifecycle:
  poll_interval_sec: 2.0
  poll_max_attempts: 3
  notional_basis: filled

# ABC pyramid · retired stub
pyramid_add:
  enabled: false
  retired_from_order: true

# C18acc pyramid · 採納
c18acc_pyramid_add:
  enabled: true
  winner_condition: underwater_rebound

# Detach · 全帳戶半砍
detach_gate:
  action:
    scope: broker_all_holdings
    mode: half_flatten
```

### 10.2 環境變數（ABC · **retired**）

| 變數 | 預設 | 說明 |
|------|------|------|
| `ABC_V3_F1_ORDER_ENABLED` | **0** | 總開關 · 必須維持 0 |
| `ABC_V3_F1_AUTO_SUBMIT` | **0** | 退役 |
| `ABC_V3_F1_CHAMPION_TP_ENABLED` | **0** | 退役 |
| `ABC_V3_F1_PYRAMID_ADD_ENABLED` | **0** | 退役 |
| `ABC_V3_F1_POSITION_AUTO_SUBMIT` | **0** | 退役 |
| `ABC_V3_F1_ORDER_FORCE_LEGACY` | 0 | 僅庫測可開 · 否則送單入口硬擋 |

### 10.3 環境變數（C18acc）

| 變數 | 預設 | 說明 |
|------|------|------|
| `ORDER_C18ACC_DRY_RUN` | 1（Book）／0（mini live） | 0 = live 送單 |
| `ORDER_C18ACC_ORDER_ENABLED` | 1 | C18 下單層總開關 |
| `ORDER_C18ACC_AUTO_SUBMIT` | 1（launchd／`.env.example`） | 1 = screen actions 自動送單（**以 env 為準** · yaml `auto_submit` 不讀） |
| `ORDER_C18ACC_APPLY_STATE` | 1 | 1 = 寫入 `data/rrg_c18acc_slots.json` |
| `ORDER_RESERVED_CASH_TWD` | 50000 | 帳戶級保留現金（多袖共用） |
| `C18ACC_BUDGET_TWD_PER_SLOT` | 20000 | 每槽預算（加碼第二筆同額當 fraction=1.0） |
| `C18ACC_CONFIRM_BARS` | **1** | live 進場確認棒數（Strategy funnel 對齊） |
| `C18ACC_PYRAMID_ADD_ENABLED` | **1** | underwater_rebound 結構加碼（O3-7 · 採納） |

### 10.4 Structural pyramid add 契約（ABC retired · C18acc 採納）

兩 block 共用 **order-pyramid-add-v1** schema；**僅 C18acc 為 live**。

| 欄位 | ABC `pyramid_add` | C18acc `c18acc_pyramid_add` |
|------|-------------------|------------------------------|
| 假設 | H-ENTRY-PYRAMID-1 | H-C18-PYRAMID-1 |
| 適用 | （retired） | `rrg-mono-swap-accel` |
| 觸發 | — | `ret_from_entry_pct<0` 且持倉 W3 RV 自 in-hold trough 反彈 ≥0.3 |
| Poll | — | 原嵌入 `rrg-c18acc-poll`（排程 2026-08-04 退役）· 僅手動 `run_c18acc_position_poll.py` |
| 部位 | — | 等權重第二筆 · `C18ACC_BUDGET_TWD_PER_SLOT` |
| 出場 | — | sync_exit · leg1 S2/sim 動態出場 |
| 預設 | `enabled: false` · `retired_from_order: true` | **`enabled: true`**（Strategy `pyramid_add.enabled: true`） |
| 證據 | historical | `20260711_c18acc_structural_pyramid_n99.md` · OOS Δ +1.189pp |

**實作狀態（2026-07-16）**：C18acc pyramid 採納並在 `rrg_mono_swap_accel_screen` 內掃加碼；ledger `data/order/c18acc_pyramid_ledger.json`。ABC monitor／radar 送單路徑已退役。

---

## 11. 測試策略

| 層級 | 範圍 | 命令 |
|------|------|------|
| 單元 | re-entry · intent · lifecycle 狀態轉換 | `pytest tests/test_abc_v3_f1_order.py` |
| 單元 | intent schema | `pytest tests/test_order_intent.py` |
| 整合 | mock Fubon submit + poll | 新增 `tests/test_order_lifecycle.py` |
| 手動 | dry-run radar | `python scripts/run_buy_signal_radar.py --no-dedup` |
| 手動 | 查委託 | `submit_intents.py --query-orders` |

---

## 12. 風險與緩解

| 風險 | 緩解 |
|------|------|
| 限價掛單未成交以為已買 | Phase 1 lifecycle → filled 才記曝險 |
| 富邦 API 503 重試 duplicate | client_intent_id + 查單 |
| ledger JSON race | atomic write / file lock（低優先） |
| ops UI scope creep | Phase 2 唯讀；送單仍在 Python |
| 術語漂移 |  prose 遵循 terminology §2.5 · §10 |

---

## 13. 里程碑

| 里程碑 | 內容 | 目標 |
|--------|------|------|
| **M0** | abc live baseline + P0/P1 修復 | ✅ 2026-07-09 |
| **M1** | Phase 1 lifecycle + notify 解耦 | T+2 週 |
| **M2** | Phase 2 ops snapshot + HTML | T+4 週 |
| **M3** | Phase 3 評估（出場 / semi-auto） | 依 live 實跑結果 |

---

## 14. 文件與交付物

| 交付 | 路徑 |
|------|------|
| 本 PRD | `docs/order-layer-prd.md` |
| 設定 | `config/order.yaml` |
| 範例 env | `.env.example` |
| 盤中減碼規則 | `docs/intraday-exit-playbook.md` |
| Ops snapshot（Phase 2） | `reports/order/snapshots/*_ops.json` |
| Agent 任務 | `docs/agent-brief.md` → 下單 · Fubon |

---

## 15. 附錄 A · 與外掛 OMS 對照（為何不自建全套）

| 能力 | OpenAlgo | 本 PRD 路線 |
|------|----------|-------------|
| React 後台 | 內建 | snapshot + 靜態 HTML / readdy |
| 30 broker | 內建 | 僅 Fubon adapter |
| Action Center | 內建 | Phase 3 可選 |
| Order lifecycle | 部分 | Phase 1 對齊 mini-OMS |
| 授權 | AGPL | 自研 MIT/ repo 既有 |

---

## 16. 附錄 B · 詞彙

| 用 | 勿用 |
|----|------|
| Order layer（下單層） | 執行層 · `layer: execution` |
| 採納 | 畢業 |
| chase_ask | 模糊「追價」無 price_type |

完整表：[terminology.md](./terminology.md) §7 · §10。

---

## 17. Shadow 驗證 checklist（champion-tp 棧 + 賣優先）

**目的**：在 **不送單給券商** 的前提下，用 1～2 個交易日驗證下單鏈路（訊號 → monitor → intent/ledger → state）行為正確，再開 live。

> **2026-07-16**：§17.2 ABC shadow **僅歷史參考**（sleeve 已退役）。現行 shadow 以 §17.3 C18acc + Leading Dip / Detach 為準。

**Shadow** = dry-run：跑真實盤中資料與邏輯，**不**呼叫 `place_order`。

### 17.1 前置（Day 0 · 盤前或收盤後）

- [ ] **單元測試全過**
  ```bash
  cd /path/to/股票研究
  python -m pytest tests/test_account_cap_gate.py \
    tests/test_c18acc_order.py \
    tests/test_c18acc_position_monitor.py -q
  ```
  預期：`N passed`，無 failed。

- [ ] **`.env` shadow 區塊**（覆寫 live 開關；可貼入 shell 或寫入 `.env`）
  ```bash
  # ABC · 必須維持退役
  ABC_V3_F1_ORDER_ENABLED=0
  ABC_V3_F1_AUTO_SUBMIT=0
  ABC_V3_F1_POSITION_AUTO_SUBMIT=0

  # C18acc
  ORDER_C18ACC_ORDER_ENABLED=1
  ORDER_C18ACC_AUTO_SUBMIT=1         # 跑 order 層邏輯（仍 dry-run）
  ORDER_C18ACC_DRY_RUN=1             # 關鍵：不送單
  ORDER_C18ACC_APPLY_STATE=0         # shadow 先不寫槽位（避免污染 state）
  C18ACC_CONFIRM_BARS=1
  C18ACC_PYRAMID_ADD_ENABLED=1
  ORDER_RESERVED_CASH_TWD=50000
  ```

- [ ] **基線快照**（方便日終 diff）
  ```bash
  ls -la data/order/c18acc_order_ledger.json \
         data/order/c18acc_pyramid_ledger.json \
         data/rrg_c18acc_slots.json 2>/dev/null || true
  ```

### 17.2 ABC 袖 · champion-tp 棧（**retired · 歷史**）

> Order sleeve 已於 2026-07-16 移除。下列僅供讀舊 ledger／庫測 `FORCE_LEGACY`；**勿**當 live checklist。

**進場（歷史）**：radar `abc-v3-f1-pullback`。**出場（歷史）**：champion TP monitor。

| # | 時點 | 命令 | 通過條件 |
|---|------|------|----------|
| A1 | — | `run_abc_v3_f1_position_poll.py` | 預期 `status: retired`（無 FORCE_LEGACY） |
| A2–A5 | — | — | 略 |

### 17.3 C18 袖 · 進換出 + 賣優先（Order layer）

| # | 時點 | 命令 | 通過條件 |
|---|------|------|----------|
| C1 | 盤中 poll 窗（09:00–13:30 · NTB≥13:00） | 見下方 C1 | `dry_run=true`；`actions` 列表合理 |
| C2 | 有 swap 時 | 檢查 `actions` 順序 | **sell 列在 buy 之前**（`max_hold_exit` / `swap sell` → `swap buy` / `entry` / `pyramid_add`） |
| C3 | `order_submit` 區塊 | C1 JSON / report | `priority: sell_first`；`results[].status` 為 `dry_run`，非 `submitted` |
| C4 | 模擬換倉買延後 | 資金刻意壓低或 mock | 出現 `deferred` + `pending_buys` 佇列（見 `data/order/c18acc_order_ledger.json`） |
| C5 | pyramid（採納） | rrg poll 內掃（或 `run_c18acc_position_poll.py`） | `kind: pyramid_add`；ledger 有 `pyramid_parent_cid` |
| C6 | 日終 | `reports/order/intents/rrg-mono-swap-accel_*.json` | 存在且 `metadata.dry_run: true` |

**C1 · C18 screen（shadow）**
```bash
PYTHONPATH=src python3 scripts/run_rrg_mono_swap_accel_screen.py \
  --date YYYY-MM-DD --dry-run --no-apply-state
```

**C5 · C18 pyramid**（已嵌 rrg poll；獨立腳本可選）
```bash
PYTHONPATH=src python3 scripts/order/run_c18acc_position_poll.py \
  --date YYYY-MM-DD --time 10:30
```

預期 stdout 片段：
```
C18acc screen: ... dry_run=True actions=N ...
```

若有 `order_submit`：
```json
{ "dry_run": true, "priority": "sell_first", "results": [{ "status": "dry_run", "side": "sell" }] }
```

### 17.4 跨袖共通

| # | 檢查項 | 通過條件 |
|---|--------|----------|
| X1 | 券商委託 | **今日無**由本 repo 腳本新產生的委託（可選：`submit_intents.py --query-orders` 人工對照） |
| X2 | `account_risk` | `config/order.yaml` · `priority_on_conflict: sell_first` · `swap_buy_retry_polls: 3` |
| X3 | Idempotency | 同一 `--date --time` 重跑兩次，第二次無重複「將送單」意圖（或 `skipped` / `idempotent`） |
| X4 | 報告 | `reports/research/rrg/` 或 `reports/order/` 有當日 screen / radar 紀錄可追 |

### 17.5 驗收簽核（滿足後才開 live）

連續 **≥1 完整交易日**（或 replay 覆蓋有持倉/換倉的 session）：

- [ ] C1–C6 全勾（C18 賣優先順序正確、未實送；pyramid 可觀測）
- [ ] X1–X4 全勾
- [ ] Leading Dip / Detach dry-run 路徑可觀測（可選同日）
- [ ] 確認：`C18ACC_CONFIRM_BARS=1` · `C18ACC_PYRAMID_ADD_ENABLED=1` · Detach **全帳戶**半砍

**開 live 最小開關**（勿跳過 shadow；ABC 維持 0）：
```bash
ABC_V3_F1_ORDER_ENABLED=0
ORDER_C18ACC_DRY_RUN=0
ORDER_C18ACC_AUTO_SUBMIT=1
ORDER_C18ACC_APPLY_STATE=1
C18ACC_CONFIRM_BARS=1
C18ACC_PYRAMID_ADD_ENABLED=1
ORDER_LEADING_DIP_DRY_RUN=0
ORDER_DETACH_GATE_DRY_RUN=0
```

建議：**先開 C18acc live 一週**，再同開 Leading Dip；Detach 為帳戶級風控，開前再確認全帳戶範圍。

### 17.6 Replay 加速（非交易日）

有持倉/換倉歷史時可用 replay 補齊 C1/C2，但仍應在下一個真實交易日跑一輪 C1 確認接線：
```bash
PYTHONPATH=src python3 scripts/run_launchd_replay.py  # 依腳本內建日期區間
```

---

## 修訂紀錄

| 版本 | 日期 | 說明 |
|------|------|------|
| 1.0 | 2026-07-09 | 初版：as-is 盤點 + Phase 0–4 藍圖 |
| 1.1 | 2026-07-11 | §17 Shadow 驗證 checklist · champion-tp + 賣優先 env 表更新 |
| 1.2 | 2026-07-16 | Live SSOT：confirm_bars=1 · C18 pyramid 採納 · ABC Order 退役文案 · Detach 全帳戶 |
