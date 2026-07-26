# Mac mini 無頭生產機 · 遷移與維運（MacBook ↔ Mac mini）

| 欄位 | 內容 |
|------|------|
| 版本 | 3.3 |
| 日期 | 2026-07-23 |
| 狀態 | **已上線**（首遷 2026-07-16；本文 = 現行 SSOT／維運手冊） |
| 最近驗收 | 2026-07-23 ≈13:40 Asia/Taipei（見 §0 · 拍板後覆寫） |
| 關聯 | [order-layer-prd.md](../../docs/order-layer-prd.md) · [daily-operations.md](../../docs/daily-operations.md) · `scripts/install-launchd.sh` · `.env.example` |

> **分工**：MacBook = 唯一 IDE／研究；手機 Cursor App = 出門遙控；**Mac mini 常開** = SQLite SSOT + Order launchd + My Machines worker。  
> **程式**：GitHub（`Book push` → `mini pull`）或同網 `rsync`。  
> **機密／`data/`**：SSH／scp／rsync（**不進 git**、不貼進 LLM 聊天）。遠端 Agent 工作流見 §1.5。

---

## 0. 現況驗收（2026-07-23）

覆寫首遷表（2026-07-16 歷史見 git）。**開關以 mini `.env` + `~/Library/Application Support/com.jackm4.etf/order.env` 為準**（launcher source 後覆寫 plist 安全預設）。

| 檢查項 | Book（MacBook Air） | Mac mini |
|--------|---------------------|----------|
| 主機名 | `JackM4deMacBook-Air` | `minim4deMac-mini` |
| 路徑／使用者 | `/Users/jackm4/Documents/ETF/股票研究` · `jackm4` | 同左 |
| `com.jackm4.etf.*` launchd | **0 支**（禁止 live） | **≈16 支已載入**（含 `order-wake`） |
| Order live（送單） | 不送單 | **C18acc** + **Leading Dip** + **Songshan copytrade** + **timed-limit** + **expert-pool-staged-gate** |
| 觀測／不下單 | — | **Detach = RED 寄信、不半砍**；buy/sell radar 不下單 |
| SSH（Book→mini） | `Host mac-mini` | 可 `BatchMode` 登入 |

**mini Order／觀測主軸（`launchctl list | grep jackm4.etf`）**

```
# 盤中 Order
com.jackm4.etf.rrg-c18acc-poll
com.jackm4.etf.leading-dip-poll
com.jackm4.etf.songshan-copytrade-poll   # live（POLL/ENABLED=1 · DRY_RUN=0 · AUTO_SUBMIT=1）
com.jackm4.etf.timed-limit-orders
com.jackm4.etf.expert-pool-staged-gate   # live
com.jackm4.etf.detach-gate               # 排程在；ORDER_ENABLED=0 · RED 只寄信
com.jackm4.etf.buy-signal-radar
com.jackm4.etf.sell-signal-radar
com.jackm4.etf.order-wake
# 夜間／輔助（不下單或預熱）
com.jackm4.etf.winbond-expert-pool-watch
com.jackm4.etf.second-disp-expert-pool-watch
com.jackm4.etf.holdings-branch-sell-monitor
com.jackm4.etf.branch-tape-prewarm
com.jackm4.etf.expert-pool-chart-digest
com.jackm4.etf.crash-thermometer-daily
com.jackm4.etf.fubon-premarket-quote-collect
```

（`fubon-premarket-quote-collect` 為研究層資料收集，**手動一次性安裝**、不在 `install-launchd.sh` `LABELS`；安裝／卸載步驟見 `scripts/launchd/fubon-premarket-quote-collect.command` 檔頭。）

**`.env`／`order.env` 對照（2026-07-23 13:40 拍板後 · 勿把密文寫進本檔）**

| 鍵 | Book | mini（現況） |
|----|------|----------------|
| `ORDER_LAUNCHD_ENABLED` | `0` | `1` |
| `RUN_RRG_C18ACC_SCREEN` / `ORDER_C18ACC_DRY_RUN` | `0` / `1` | `1` / `0`（live） |
| `RUN_LEADING_DIP_POLL` / `ORDER_LEADING_DIP_DRY_RUN` | `0` / `1` | `1` / `0`（live） |
| `RUN_SONGSHAN_COPYTRADE_POLL` / `ENABLED` / `DRY_RUN` / `AUTO_SUBMIT` | 關 | **`1` / `1` / `0` / `1`（live）** |
| `RUN_TIMED_LIMIT_ORDERS` / `ORDER_TIMED_LIMIT_DRY_RUN` | — | `1` / `0`（live once） |
| `RUN_EP_STAGED_GATE` / `ORDER_EP_STAGED_GATE_DRY_RUN` | — | `1` / `0`（live） |
| `RUN_DETACH_GATE` / `ORDER_DETACH_GATE_ORDER_ENABLED` / `RUN_DETACH_GATE_EMAIL` | 關 | `1` / **`0`（不送單）** / **`1`（RED 寄信）** |
| `ABC_V3_F1_ORDER_ENABLED` | `0` | `0` |

Book 雙保險：各袖 `RUN_*=0` + dry-run（見 §4.7）。

**Ledger／當日備註（以 mini `data/order/` 為準）**

| 袖 | 備註 |
|----|------|
| Songshan | 07-23 上午 live 試買 **2492** → 全額處置拒單 → burn（**接受不重試**）；12:06 誤關 → **13:40 恢復 live**（下一交易日 09:25 起生效） |
| EP staged gate | 07-23 買入 **2327**／**8046** 零股（filled）· **維持 live** |
| C18acc / Leading Dip | live |
| Detach | **只寄信**：`ORDER_ENABLED=0` · `RUN_DETACH_GATE_EMAIL=1`（RED 達標 notify · 不半砍） |

**運維決策（2026-07-23 拍板）**

| 項 | 決定 |
|----|------|
| 松山 12:06 關機 | **誤關** → 已恢復 live 四鍵 |
| 全額處置 burn | **接受** |
| `hold_days=7` | **暫只進場**（無自動賣出） |
| Detach | **寄信即可** · 不送單 |
| EP staged gate | **維持 live** |

**注意**

1. plist 內 Songshan 預設 `DRY_RUN=1`／`AUTO_SUBMIT=0` 是安全預設；真正開關在 `order.env`／`.env`。  
2. Book／mini 的 `data/order/*.json` **不同 inode**；生產寫入只在 mini。  
3. 改 `.env` 後須同步 `order.env`（`install-launchd.sh` 或手動 upsert），否則 launcher 仍讀舊值。

**運維事件（2026-07-26）**

- 稽核發現 `com.jackm4.etf.order-chase-open` 仍掛在 mini launchd 上（`ORDER_LAUNCHD_ENABLED=1`，閘門開），與本文「不裝」的決策不符；根因是舊版 `install-order-launchd.sh` 靠 §4.6 手動 bootout 步驟撤除，某次重跑安裝腳本（例如加裝 Songshan／EP staged gate 等較新 order 功能時）沒有重做這一步就又裝回去。
- 處理：SSH 到 mini 執行 `bootout` + 刪除 plist，即時撤除；同時把 `install-order-launchd.sh` 改成 `order-chase-open` 進 `LEGACY_LABELS`，之後每次執行安裝腳本都會自動卸載，不再依賴人工步驟。

---

## 1. 決策摘要（現行）

### 1.1 機器

| 項目 | 決策 |
|------|------|
| 生產機 | **Mac mini**（無頭；**不裝 Cursor App**；可裝 Cursor CLI + My Machines worker） |
| 研究機 | **MacBook**（唯一 IDE · 平常主入口） |
| 手機遙控 | **Cursor iOS App**（或 `cursor.com/agents`）→ worker=`mac-mini` |
| 路徑 | `/Users/jackm4/Documents/ETF/股票研究`（兩台相同） |
| 使用者 | `jackm4` |
| 網路 | mini **乙太網**；Book Wi‑Fi；同網 SSH 別名 `mac-mini`（見 §3） |
| Tailscale | **建議**（出國 SSH 備用）；日常手機／Book 指揮 mini **不需**同 Wi‑Fi（worker outbound） |
| 雙機並跑 | **禁止**（Book 卸載全部 `com.jackm4.etf.*` · **含** cursor-agent-worker） |
| Python | **3.13** · `.venv`（策略）+ `.venv-fubon`（富邦 wheel） |
| DB SSOT | **僅 mini** `data/stocks.db`；Book 只讀 replica |
| Order SSOT | **僅 mini** `data/order/` · `reports/order/` · `logs/intraday/` |

### 1.2 Order layer launchd（盤中 + 夜間 + 防睡）

與 `scripts/install-launchd.sh` 的 `LABELS` 一致。**ABC 已自 Order 移除**（2026-07-16；送單入口硬擋 · buy-radar ABC 觀察軌關閉）；`buy-signal-radar` 其餘軌 **只觀察／寄信，不送單**。

**送單現況以 §0 為準**（下表「角色」= 規格；括號標運維狀態）。

| # | Job | 時間 | 角色 |
|---|-----|------|------|
| 1 | `rrg-c18acc-poll` | 週一至五 09:00–13:30 每 5 分 | C18acc 主袖：換倉／進場／袖內退場 · **live** |
| 2 | `leading-dip-poll` | 週一至五 09:05–13:25 每 5 分 | Leading Dip 衛星袖 · **live**（與 C18 互斥） |
| 2b | `songshan-copytrade-poll` | 週一至五 09:25–09:40 每 5 分 | 跟單松山（凱基 `9217`）· 5d淨比95∩!mega + 25m nonfail · **買 1 張** · **live** |
| 2c | `timed-limit-orders` | 週一至五 09:05 | `config/order.yaml` timed_limit_orders · **live once** |
| 2d | `expert-pool-staged-gate` | 週一至五 09:00／01／05／25 | 專家池 gap→05→25 · **live** · **≠** 松山五日尺 |
| 3 | `buy-signal-radar` | 週一至五 09:00–13:20 每 5 分 | 買訊觀察／通知 · **不送單** |
| 4 | `sell-signal-radar` | 週一至五 09:06–13:20 每 5 分 | 過熱／extension advisory · 通知為主 |
| 5 | `detach-gate` | 週一至五 09:40–12:30 每 5 分 | 台美脫鉤 · **RED 寄信 · 不半砍**（`ORDER_ENABLED=0`） |
| + | `order-wake` | 週一至五 08:55 | 防睡眠（`install-order-launchd.sh`） |
| 6 | `winbond-expert-pool-watch` | 週一至五 **20:00** | 專家池＋松山／新店 **買方**共識 digest · **不下單** |
| 6b | `second-disp-expert-pool-watch` | 週一至五 **20:35** | 處置股專家池 · T0 濾網 · email · **不下單** |
| 7 | `holdings-branch-sell-monitor` | 週一至五 **20:10** | 富邦持倉×專家分點 **淨賣**預警 · email · **不下單** |

#### 跟單松山（Copytrade）規格摘要

| 項 | 值 |
|----|-----|
| Strategy id | `songshan-copytrade`（`config/order.yaml`） |
| 分點 | 凱基-松山 `9217` |
| 訊號 | 昨交易日：五日買 ≥ 0.5億 ∩ 淨比 ≥ 0.95 ∩ !mega |
| 進場 | T+1 09:25–09:40 · live `entry_25m_nonfail` · `chase_ask` · **預算制 約10萬台幣**（`qty = budget_twd / 買一價`；<1000股走零股、≥1000股走整股，2026-07-24 起，見 `docs/songshan-copytrade-budget-migration.md`；舊版固定 1000 股/1張 已停用） |
| 出場 | `hold_days: 7` **僅參數** · **無自動賣出腳本**（暫接受只進場） |
| 夜信同尺 | `RUN_SONGSHAN_FOLLOW_EMAIL`（併入 20:00 digest · 不下單） |
| live 四鍵 | `RUN_SONGSHAN_COPYTRADE_POLL=1` · `ORDER_SONGSHAN_COPYTRADE_ENABLED=1` · `DRY_RUN=0` · `AUTO_SUBMIT=1` |

一句對照：

| 排程 | 角色 |
|------|------|
| C18acc poll | 自動買／換／賣（主袖） |
| Leading Dip poll | 自動買／賣（衛星袖） |
| Songshan copytrade | 昨訊號 → T+1 09:25 nonfail 買約10萬台幣預算（零股/整股，live） |
| timed-limit-orders | 09:05 限價單 · 逾時撤 |
| expert-pool-staged-gate | 專家池 gap／05／25（≠ 松山） |
| Buy／Sell radar | 觀察，不送單 |
| Detach Gate | RED 達標寄信 · **不半砍**（`ORDER_ENABLED=0`） |
| 20:00／20:35／20:10 | 買方／處置／賣方觀測 |

Log：`logs/intraday/launchd_*.log` · Songshan 另見 `logs/intraday/songshan_copytrade_YYYYMMDD.log`。

### 1.3 已退役（不再掛 launchd）
```
com.jackm4.etf.morning-holdings-brief   # 手動：scripts/order/morning_holdings_brief.py
com.jackm4.etf.evening-holdings
com.jackm4.etf.intraday-exit-gate       # 結構停損閘門 · 退回 Research
com.jackm4.etf.intraday-*-digest
com.jackm4.etf.rrg-mono-intraday-watch
com.jackm4.etf.vcp-funnel-specs
com.jackm4.etf.minervini-sepa-basket
com.jackm4.etf.mutual-fund-disclosure-watch
com.jackm4.etf.weekly-deep
com.jackm4.etf.order-chase-open
com.jackm4.etf.c18acc-extension-overlay
```

ABC v3+F1：**已自 Order 移除**（`.env` 全部 `ABC_V3_F1_*=0` · ledger 僅供 Leading Dip cross-exclude）。Buy radar 不再跑 ABC 觀察軌。

### 1.4 通知（Email）

| 通知 | 要？ |
|------|------|
| C18acc poll 動作 | ✓ `RUN_RRG_C18ACC_EMAIL` |
| Leading Dip 送單 | ✓ `RUN_LEADING_DIP_SUBMIT_EMAIL` |
| Songshan copytrade | ✓ `ORDER_SONGSHAN_COPYTRADE_SUBMIT_EMAIL`（活動 tick） |
| Buy radar 命中 | ✓ `RUN_BUY_SIGNAL_EMAIL` |
| Sell radar | ✓ `RUN_SELL_SIGNAL_EMAIL` |
| Detach Gate | ✓ `RUN_DETACH_GATE_EMAIL` |
| 20:00 專家池＋松山 digest | ✓ `RUN_WINBOND_EXPERT_POOL_EMAIL`／`RUN_SONGSHAN_FOLLOW_EMAIL` |
| 20:10 持倉分點淨賣 | ✓ `RUN_HOLDINGS_BRANCH_SELL_EMAIL` |
| ABC submit | ✗ 已退役 |

Gmail：`GMAIL_USER` + App Password。

### 1.5 遠端 Agent 工作流（Book 平常 · 手機出門 · mini 常開）

```
┌─────────────────┐     ┌──────────────────┐     ┌────────────────────────────┐
│ MacBook（研究）  │     │ 手機 Cursor App   │     │ Mac mini（生產 · 常開）      │
│ Cursor IDE      │     │ Plan/ask/build…   │     │ Order launchd（送單／雷達）  │
│ 本地 Agent      │     │ → worker=mac-mini │     │ agent worker KeepAlive      │
│ 不裝 live Order │     │ 不需同 Wi‑Fi      │     │ 工具呼叫落點：本機 data/log │
└─────────────────┘     └──────────────────┘     └────────────────────────────┘
         │                        │                         ▲
         │                        └──── Cursor 雲端（模型）──┘
         └──── 本地工具（研究碼／replica；不碰 live ledger）
```

| 場景 | 入口 | 工具跑在哪 |
|------|------|------------|
| 平常改碼／研究／回測 | Book Cursor IDE | **Book** |
| 出門查盤中 log／launchd／ledger 狀態 | 手機 Cursor App · 選 **mac-mini** | **mini** |
| 人在外地、Book 也要摸生產 | Book 開 Cloud Agent · 選 **mac-mini** | **mini** |
| 純 GitHub 改碼／開 PR（不需生產資料） | 手機／Book Cloud Agent（預設雲 VM） | Cursor 雲 VM（**無** mini `data/`） |
| 裝機／worker 掛了 | Screen Sharing／同網 SSH · mini CLI | mini |

**一次性啟用（僅 mini）**

1. CLI 已裝：`~/.local/bin/agent`（`curl https://cursor.com/install -fsS | bash`）。
2. **ASCII symlink**（exec-daemon 無法處理路徑中的非 Latin-1 字元）：

```bash
ln -sfn ~/Documents/ETF/股票研究 ~/etf-stocks
```

3. 認證二選一：
   - **推薦**：在 mini 的 Aqua／launchd 環境跑 `agent login`（SSH 會卡 Keychain；用 Screen Sharing 或本安裝腳本觸發的 login）。
   - 或 [Integrations](https://cursor.com/dashboard/integrations) 個人 API key 寫入 mini `.env` 的 `CURSOR_API_KEY`。
4. mini `.env`：

```bash
CURSOR_AGENT_WORKER_ENABLED=1
CURSOR_AGENT_WORKER_NAME=mac-mini
CURSOR_AGENT_WORKER_DIR=/Users/jackm4/etf-stocks
# CURSOR_API_KEY=...   # 若未 agent login 才需要
```

5. 安裝 KeepAlive：

```bash
ssh mac-mini 'cd ~/Documents/ETF/股票研究 && bash scripts/install-cursor-agent-worker-launchd.sh --status'
```

6. 手機：Add Workspace → 本 repo → 開 Agent 時環境選 **mac-mini**。

**護欄**

- Book **禁止** `install-cursor-agent-worker-launchd.sh`。
- Agent 勿 `cat .env`／讀 `CAFubon/`；優先 `scripts/launchd_status_dashboard.sh`、tail `logs/intraday/`。
- 出國 SSH 備用：Tailscale（§5.4）；worker 日常指揮 **不依賴** Tailscale。

---

## 2. 架構

```
MacBook（研究機 · 唯一 IDE）                Mac mini（無頭生產機）
─────────────────────────────────            ─────────────────────────────────
改 src/ config launchd 範本                    常開 · 乙太網 · 已登入
pytest / 小回測（讀 replica）                  WRITE SSOT：data/stocks.db
git push → GitHub  ←──────── git pull ───────── launchd：Order 袖 + radar + 夜間
SSH → 遙控部署與看 log                         live：C18 + Leading Dip + Songshan
本地 Agent（研究）                             + EP gate + timed-limit
無 live Order／無 agent-worker                 Detach：RED 寄信 · 不送單
                                               cursor-agent-worker（My Machines）
手機 Cursor App ── worker=mac-mini ──────────► 工具呼叫落在 mini
```
---

## 3. SSH（Book → mini）

`~/.ssh/config`（Book）：

```
Host mac-mini
    HostName 192.168.1.102
    User jackm4
    IdentityFile ~/.ssh/id_ed25519_smartboss
    IdentitiesOnly yes
```

```bash
ssh mac-mini 'hostname; launchctl list | grep jackm4.etf'
```

首次灌公鑰：`ssh-copy-id -i ~/.ssh/id_ed25519_smartboss.pub jackm4@192.168.1.102`  
（HostName 若 DHCP 變更，只改 config 的 `HostName`。）

---

## 4. 首次灌機（已完成 · 重灌時照做）

### 4.1 系統

- 接電、防睡眠：`sudo pmset -c sleep 0 disksleep 0 displaysleep 10`
- 遠端登入（SSH）、交易日已登入
- Xcode CLT + Homebrew + `python@3.13` + `git`
- 若外網異常慢：先比對 IPv4／IPv6（曾見 IPv6 半速）；必要時暫時 Ethernet IPv6 → Link-Local

### 4.2 程式與機密

建議同網整包 rsync（含 `.git`；排除本機 venv）：

```bash
# Book
rsync -az --progress -e "ssh -o BatchMode=yes" \
  --exclude '.venv/' --exclude '.venv-fubon/' \
  --exclude '__pycache__/' --exclude '.pytest_cache/' \
  "/Users/jackm4/Documents/ETF/股票研究/" \
  "mac-mini:Documents/ETF/股票研究/"
```

必含：`.env`、`CAFubon/`、`data/`（含 `stocks.db`、C18 slots、Leading Dip／Detach ledger）。  
**勿** commit `.env`／`CAFubon/`／`data/`。

### 4.3 venv

```bash
ssh mac-mini 'bash -lc "
eval \"\$(/opt/homebrew/bin/brew shellenv)\"
cd ~/Documents/ETF/股票研究
python3.13 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -r requirements.txt
python3.13 -m venv .venv-fubon && .venv-fubon/bin/pip install -U pip
.venv-fubon/bin/pip install CAFubon/fubon_neo-*-macosx_*_arm64.whl PyYAML
"'
```

### 4.4 Mac mini `.env` 片段（生產）

在已複製的 `.env` 上確認（其餘 FinMind／Gmail／Supabase 等保留）：

```bash
# === C18acc live ===
RUN_RRG_C18ACC_SCREEN=1
ORDER_C18ACC_ORDER_ENABLED=1
ORDER_C18ACC_AUTO_SUBMIT=1
ORDER_C18ACC_DRY_RUN=0
ORDER_C18ACC_APPLY_STATE=1
RUN_RRG_C18ACC_EMAIL=1
C18ACC_KBAR_SYNC=1

# === Leading Dip live ===
RUN_LEADING_DIP_POLL=1
RUN_LEADING_DIP_SUBMIT_EMAIL=1
ORDER_LEADING_DIP_ORDER_ENABLED=1
ORDER_LEADING_DIP_DRY_RUN=0
ORDER_LEADING_DIP_AUTO_SUBMIT=1

# === Detach Gate（RED 寄信 · 不半砍）===
RUN_DETACH_GATE=1
RUN_DETACH_GATE_EMAIL=1
ORDER_DETACH_GATE_ORDER_ENABLED=0
ORDER_DETACH_GATE_DRY_RUN=1
ORDER_DETACH_GATE_AUTO_SUBMIT=0

# === 跟單松山 live ===
RUN_SONGSHAN_COPYTRADE_POLL=1
ORDER_SONGSHAN_COPYTRADE_ENABLED=1
ORDER_SONGSHAN_COPYTRADE_DRY_RUN=0
ORDER_SONGSHAN_COPYTRADE_AUTO_SUBMIT=1
ORDER_SONGSHAN_COPYTRADE_QTY=1000
ORDER_SONGSHAN_COPYTRADE_SUBMIT_EMAIL=1

# === 專家池漏斗閘門 live ===
RUN_EP_STAGED_GATE=1
ORDER_EP_STAGED_GATE_ENABLED=1
ORDER_EP_STAGED_GATE_DRY_RUN=0

# === 限時限價（once jobs）===
RUN_TIMED_LIMIT_ORDERS=1
ORDER_TIMED_LIMIT_DRY_RUN=0

# === Radars（不送單）===
RUN_BUY_SIGNAL_RADAR=1
RUN_BUY_SIGNAL_EMAIL=1
RUN_SELL_SIGNAL_RADAR=1
RUN_SELL_SIGNAL_EMAIL=1

# === ABC Order 退役 ===
ABC_V3_F1_ORDER_ENABLED=0
ABC_V3_F1_AUTO_SUBMIT=0
ABC_V3_F1_CHAMPION_TP_ENABLED=0
ABC_V3_F1_PYRAMID_ADD_ENABLED=0
ABC_V3_F1_POSITION_AUTO_SUBMIT=0
RUN_ABC_V3_F1_SUBMIT_EMAIL=0

ORDER_LAUNCHD_ENABLED=1
```

完整鍵名見 `.env.example`。改完 `.env` 後執行 `scripts/install-launchd.sh`（或同等）重寫 `order.env`，否則 launcher 仍讀舊開關。

### 4.5 健康檢查（上線前／重灌後）

```bash
ssh mac-mini 'bash -lc "
cd ~/Documents/ETF/股票研究
export ROOT=\"\$(pwd)\" PYTHONPATH=src
.venv-fubon/bin/python scripts/order/fubon_login_test.py --snapshot
# C18 實際入口（計劃舊名 run_rrg_c18acc_screen.py 已廢）
RUN_RRG_C18ACC_SCREEN=1 ORDER_C18ACC_DRY_RUN=1 \
  .venv/bin/python scripts/run_rrg_mono_swap_accel_screen.py
RUN_BUY_SIGNAL_RADAR=1 .venv/bin/python scripts/run_buy_signal_radar.py
"'
```

盤前時段 C18／radar 可能 `skip: outside … window`（exit 0）屬正常。

### 4.6 安裝 launchd（僅 mini）

```bash
ssh mac-mini 'bash -lc "
cd ~/Documents/ETF/股票研究
bash scripts/install-launchd.sh
bash scripts/install-order-launchd.sh
bash scripts/install-launchd.sh --status
launchctl list | grep jackm4.etf
"'
```

開盤窗追價（`order-chase-open`）**不裝**：`install-order-launchd.sh` 把它列在 `LEGACY_LABELS`，每次執行都會自動 `bootout` + 刪除其 plist，不需要再手動撤（2026-07-26 前的版本靠手動步驟，曾因此在 mini 上重跑安裝腳本後意外復活過一次，已改成自動清除）。

預期載入：`rrg-c18acc-poll` · `leading-dip-poll` · `buy-signal-radar` · `sell-signal-radar` · `detach-gate` · `order-wake`。

### 4.7 MacBook 停用 live

```bash
cd ~/Documents/ETF/股票研究
bash scripts/install-launchd.sh --uninstall
bash scripts/install-order-launchd.sh --uninstall
launchctl list | grep jackm4.etf || echo "OK: none"
```

Book `.env` 建議（研究機 · 雙保險：閘門關 + dry-run）：

```bash
ORDER_LAUNCHD_ENABLED=0
RUN_RRG_C18ACC_SCREEN=0
ORDER_C18ACC_DRY_RUN=1
RUN_LEADING_DIP_POLL=0
ORDER_LEADING_DIP_DRY_RUN=1
RUN_DETACH_GATE=0
ORDER_DETACH_GATE_DRY_RUN=1
RUN_BUY_SIGNAL_RADAR=0
RUN_SELL_SIGNAL_RADAR=0
ABC_V3_F1_ORDER_ENABLED=0
ABC_V3_F1_AUTO_SUBMIT=0
ABC_V3_F1_CHAMPION_TP_ENABLED=0
ABC_V3_F1_PYRAMID_ADD_ENABLED=0
ABC_V3_F1_POSITION_AUTO_SUBMIT=0
RUN_ABC_V3_F1_SUBMIT_EMAIL=0
RUN_SUPABASE_*=0
```

之後 **勿**在 Book 再 `install-launchd.sh`（除非暫時除錯且已關 mini live）。  
Book `.env` 已於 2026-07-16 套用上表（Leading Dip／Detach `DRY_RUN=1`）。

---

## 5. 日常維運

### 5.1 程式同步

```bash
# Book
git push

# mini（交易日開盤前，或從 Book 代拉）
ssh mac-mini 'cd ~/Documents/ETF/股票研究 && git pull'
# 或未 push 的工作樹：同網 rsync（§4.2）
# 本文件若未進 git：另 rsync deploy/mac-mini/MIGRATION_PLAN.md
```

Ledger／slots／`stocks.db`／`logs/intraday/` **以 mini 為準**，無需回寫 Book。

### 5.2 盤中監看

```bash
ssh mac-mini 'cd ~/Documents/ETF/股票研究 && scripts/launchd_status_dashboard.sh'
ssh mac-mini 'tail -f ~/Documents/ETF/股票研究/logs/intraday/launchd_rrg-c18acc-poll.log'
ssh mac-mini 'tail -f ~/Documents/ETF/股票研究/logs/intraday/launchd_leading-dip-poll.log'
ssh mac-mini 'tail -f ~/Documents/ETF/股票研究/logs/intraday/launchd_buy-signal-radar.log'
ssh mac-mini 'tail -f ~/Documents/ETF/股票研究/logs/intraday/launchd_sell-signal-radar.log'
ssh mac-mini 'tail -f ~/Documents/ETF/股票研究/logs/intraday/launchd_detach-gate.log'
ssh mac-mini 'tail -f ~/Documents/ETF/股票研究/logs/launchd_order-wake.log'
```

快速重驗（開盤後／異動後）：

```bash
# Book 應無 agents；mini 應有 6 支
launchctl list | grep jackm4.etf || echo "Book OK: none"
ssh mac-mini 'launchctl list | grep jackm4.etf'
```

### 5.3 Book 拉 DB 複本（可選）

```bash
# mini
sqlite3 data/stocks.db "VACUUM INTO 'data/replica_export/stocks_snap.db';"
# Book
rsync -az --progress mac-mini:Documents/ETF/股票研究/data/replica_export/stocks_snap.db \
  data/replica/stocks.db
```

### 5.4 後續 Phase（未完成）

| Phase | 內容 |
|-------|------|
| Tailscale | 出國穩定 SSH（worker 日常不依賴；仍建議裝） |
| cursor-agent-worker | mini KeepAlive My Machines（§1.5；待填 `CURSOR_API_KEY` 後安裝） |
| DB replica 排程 | 自動 `VACUUM INTO` + rsync |
| `rrg_poll_features` | 特徵預計算，減輕 poll 內重算 |

---

## 6. 風險

| 風險 | 緩解 |
|------|------|
| 雙機同時送單 | Book 無 launchd；僅 mini live；Book 全袖 `RUN_*=0` + dry-run |
| 誤開 ABC | mini／Book `.env` 全 `ABC_V3_F1_*=0`；Buy radar 只觀察 |
| Book 誤開 poll | Leading Dip／Songshan／Detach 亦設 `DRY_RUN=1` 或 `ENABLED=0`（§4.7／§0） |
| 睡眠錯過 tick | 乙太網 + `pmset` + `order-wake` + 接電 |
| SQLite／ledger 雙開 | Book 只讀 replica；Order SSOT 僅 mini |
| 密文進 git／chat | scp／rsync 檔案；Agent 不 echo 密文 |
| 手機 Agent 摸到 live | 選對 worker；禁止讀 `.env`／`CAFubon/`；Book 不裝 worker |
| worker Keychain 鎖 | mini 用 `CURSOR_API_KEY`（§1.5），勿依賴 SSH 上 `agent login` |

---

## 7. 相關

- `scripts/install-launchd.sh` — Order job SSOT  
- `scripts/install-order-launchd.sh` — `order-wake`  
- `scripts/install-cursor-agent-worker-launchd.sh` — My Machines worker（**僅 mini**）  
- `scripts/launchd_status_dashboard.sh` — 盤前／盤中健檢  
- `.env.example` — 環境變數 SSOT  
- [docs/order-layer-prd.md](../../docs/order-layer-prd.md) — Order layer（下單層）  
- [docs/daily-operations.md](../../docs/daily-operations.md) — 日常操作  

**已刪**：`deploy/imac/`（iMac 轉向 stub · 2026-07-16）。
