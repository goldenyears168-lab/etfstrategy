# Mac mini 無頭生產機 · 遷移與維運（MacBook ↔ Mac mini）

| 欄位 | 內容 |
|------|------|
| 版本 | 3.3 |
| 日期 | 2026-07-23 |
| 狀態 | **已上線**（首遷 2026-07-16；本文 = 現行 SSOT／維運手冊） |
| 最近驗收 | 2026-07-23 ≈13:40 Asia/Taipei（見 §0 · 拍板後覆寫） |
| 關聯 | [order-layer-prd.md](../../docs/order-layer-prd.md) · [daily-operations.md](../../docs/daily-operations.md) · `scripts/install-launchd.sh` · `.env.example` |

> **分工**：MacBook = 唯一 IDE／研究；出門遙控 = SSH `mac-mini`（同網／Tailscale）；**Mac mini 常開** = SQLite SSOT + Order launchd。  
> **程式**：GitHub（`Book push` → `mini pull`）或同網 `rsync`。  
> **機密／`data/`**：SSH／scp／rsync（**不進 git**、不貼進 LLM 聊天）。遠端存取見 §1.5。

---

## 0. 現況驗收（2026-07-23）

覆寫首遷表（2026-07-16 歷史見 git）。**開關以 mini `.env` + `~/Library/Application Support/com.jackm4.etf/order.env` 為準**（launcher source 後覆寫 plist 安全預設）。

| 檢查項 | Book（MacBook Air） | Mac mini |
|--------|---------------------|----------|
| 主機名 | `JackM4deMacBook-Air` | `minim4deMac-mini` |
| 路徑／使用者 | `/Users/jackm4/Documents/ETF/股票研究` · `jackm4` | 同左 |
| `com.jackm4.etf.*` launchd | **0 支**（禁止 live） | **≈16 支已載入**（含 `order-wake`） |
| Order live（送單） | 不送單 | **C18acc** + **Leading Dip** + **Songshan copytrade** + **expert-pool-staged-gate** |
| 觀測／不下單 | — | **Detach = RED 寄信、不半砍**；buy/sell radar 不下單 |
| SSH（Book→mini） | `Host mac-mini` | 可 `BatchMode` 登入 |

**mini Order／觀測主軸（`launchctl list | grep jackm4.etf`）**

```
# 盤中 Order
com.jackm4.etf.rrg-c18acc-poll
com.jackm4.etf.leading-dip-poll
com.jackm4.etf.songshan-copytrade-poll   # live（POLL/ENABLED=1 · DRY_RUN=0 · AUTO_SUBMIT=1）
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
- `timed-limit-orders`（限時限價單，09:05 once-date 排程）確認之後不會再用，整支移除：mini 撤 launchd + 刪 plist/wrapper + 清 `data/order/timed_limit_orders_state.json`；Book 端刪除 `src/order/timed_limit_order.py`、`scripts/order/run_timed_limit_orders.py`、對應 launchd 樣板與 `install-launchd.sh` 註冊、`config/order.yaml` 的 `timed_limit_orders` 清單、`.env`/`.env.example`/`order.env` 的 `*_TIMED_LIMIT_*` 旗標。共用函式 `_order_still_open`（EP staged gate 也在用）先搬到 `order/fubon_orders.py`（改名 `order_still_open`，公開函式）才刪整個模組，避免誤傷還在跑的 EP staged gate。

**運維事件（2026-07-29 · Order 層全關 + 架構簡化 + 地基重整開工）**

- **Order 層全數暫停**：5支具下單能力的 job（`rrg-c18acc-poll`／`leading-dip-poll`／`songshan-copytrade-poll`／`expert-pool-staged-gate`／`detach-gate`）全部確認 `.env` 旗標安全（`DRY_RUN=1`／`ORDER_ENABLED=0`／`AUTO_SUBMIT=0`）**且** `launchctl disable`（重開機不會復活）。§0 表格與 §1.2 的「live」標註已過時，目前**無一支**實際具下單能力。
- **簡化為「只留分點研究＋網站基礎設施」**：另暫停 `buy-signal-radar`／`sell-signal-radar`（ABC退役殘留、無下單能力純clutter）、`order-wake`（服務對象已全暫停）。維持運作：`branch-tape-prewarm`／`winbond-expert-pool-watch`／`second-disp-expert-pool-watch`／`expert-pool-chart-digest`／`holdings-branch-sell-monitor`／`second-disp-oos-accumulate`／`crash-thermometer-daily`／`fubon-premarket-quote-collect`／`fubon-intraday-quote-collect`／`ops-live-ta-poll`／`ops-console-evening-sync`，新增 `live-ta-kbar-sync`（週一至五14:00，動態 Live TA universe 的 `stock_kbar_1m` 每日增補，見 `reports/research/intraday_direction_thermometer/LIVE_TA_FIELD_OPTIMIZATION_20260728.md`）。
- **`cursor-agent-worker` 退役**：查log自2026-07-24啟用以來未見任何實際派工紀錄，判斷閒置未用；已 `CURSOR_AGENT_WORKER_ENABLED=0` + `launchctl disable`。取代方案：Claude Code雲端排程routine `expert-pool-branch-health-check`（週一至五21:00台北，Supabase MCP唯讀健檢分點研究資料新鮮度＋Gmail報告；與既有的 `daily-foreign-rotation-commentary` 職責切開，互不重疊）。§1.5 已改寫為「遠端存取」（cursor-worker 相關段落移除）。
- **地基重整 Phase 3 收盤驗證（2026-07-30 13:40）**：兩個交易日的排程job（07-29晚間全批 branch-tape-prewarm/crash-thermometer/evening_research_watch/expert-pool-chart-digest/holdings-branch-sell/live-ta-kbar-sync/ops-console-evening-sync/second-disp-expert-pool-watch + 07-30當天collector）逐一核對log時間戳，**全部正確寫進新路徑 `~/goldenstocks-data`**，舊路徑對應的log目錄完全凍結（自07-29 cutover後無新檔案）。**但發現舊 `data/stocks.db` 本身在07-30當天仍有成長**（+3.4MB、mtime 12:37，非launchd job造成——所有collector自己的log都乾淨指向新路徑）。查證：`GOLDENSTOCKS_DATA_DIR` 只透過 `launchctl setenv` 對Aqua/launchd網域生效，**互動式SSH登入shell不會繼承這個值**，任何用手動SSH直接跑的腳本（非launchd job）會悄悄退回舊路徑預設值。已修復：`~/.zshrc` 加上 `export GOLDENSTOCKS_DATA_DIR=$HOME/goldenstocks-data`，讓互動session也一致。抽查 `stock_daily_bars`（07-29起的新資料）確認新db是嚴格superset（0筆只存在舊db），研判不是資料流失，但**在此修復生效、再確認一天前，暫緩刪除舊殘留備份**（原訂07-30執行，順延）。
- **地基重整開工（Phase 1 已完成，Phase 3-4 待排）**：發現 `.env`／`data/stocks.db`／`logs/` 混在 git working tree 裡是deploy風險根因（每次 `git pull` 都可能踩到本地未commit修改，2026-07-28曾實際發生一次stash攻防）。Phase 1（Book，程式碼支援 `GOLDENSTOCKS_DATA_DIR` 環境變數覆寫資料/機密路徑，未設＝完全等同現行行為）已完成並測試通過。Phase 2（本節）。**Phase 3（mini：把 `.env`/`data`/`logs`/`CAFubon` 搬到 `~/goldenstocks-data/`）與 Phase 4（mini＋Book：repo checkout 本身遷離 `~/Documents/ETF/股票研究` 中文路徑，改到 `~/goldenstocks`）尚未執行，會在收盤後分階段做並逐步驗證**。完整計畫見 session記錄；執行前本文件§0現況表會再更新一次反映實際新路徑。
- **命名決策（2026-07-29 下午）**：用戶決定專案不再侷限於「跟單ETF」定位，正式改名為 **goldenstocks**。範圍分兩輪：**今輪（低風險，隨Phase 3-4一起做）**＝本地目錄名稱（`~/goldenstocks`／`~/goldenstocks-data`）＋程式碼內部命名（`GOLDENSTOCKS_DATA_DIR`）＋文件文字；**下一輪（高風險，另外處理，不隨今天進度）**＝GitHub repo改名（`goldenyears168-lab/etfstrategy`→`goldenstocks`）、mini上所有launchd job的識別字首（`com.jackm4.etf.*`→`com.jackm4.goldenstocks.*`，含Application Support／Logs路徑）、網站網域（`haoshi-quant-ops.pages.dev`）。這幾項對外部服務／目前還在跑的job有實際改動風險，刻意不跟今天的基礎重整綁在一起做。
- **Phase 3 二次收盤驗證（2026-07-31 13:40）再延後一輪**：確認舊 `data/stocks.db` 自07-30 12:37起完全凍結（byte-exact），07-30互動式shell修復生效。但追查另外發現**4支active job腳本各自獨立寫死`ROOT/data`路徑，完全繞過`GOLDENSTOCKS_DATA_DIR`機制**（不是env沒設的問題，是code本身沒接這個機制）：`run_market_crash_thermometer_dashboard.py`（crash-thermometer-daily，09:00，`DB_PATH`直接寫死、不經`DEFAULT_DB_PATH`——這支一直在讀舊的、逐漸過期的價格資料）、`run_evening_research_watch_digest.py`／`run_expert_pool_watch.py`／`run_songshan_follow_watch.py`（winbond-expert-pool-watch，20:00，`--state-dir`預設寫死，只是dedup用的state檔、非財務資料）。已改成從`stock_db`匯入`DATA_DIR`/`DEFAULT_DB_PATH`，commit `8f9fee2`，mini已pull。**這4支今天都已經跑過舊版**（crash-thermometer 09:00, winbond 20:00尚未到），所以今晚才是第一次用新版跑；順延到**下週一(08-03)收盤後**再檢查一次舊db是否真的完全停止成長，才刪除殘留備份。
- **另發現 `chip-macro-tracker` job（非本次遷移安裝）**：mini上已裝、weekdays 20:00執行，屬於另一條並行進行中的研究工作（`research/dashboard-completeness` branch），其`daily_tracker.py`的`PANEL_DIR`同樣寫死`ROOT/data`、尚未套用`GOLDENSTOCKS_DATA_DIR`——**這是別人的進行中程式碼，這次刻意不動它**，只記錄在`config/job_registry.yaml`供之後协調。這也代表：只要這支job還沒改，舊`data/`目錄底下至少`data/research/chip_macro/`這個子目錄本身會持續有新檔案，**刪除舊殘留備份前要跟該工作的owner確認**，不能只看`stocks.db`本身凍結就視為全部安全。

**運維事件（2026-08-01 · 全路徑健檢 + research/dashboard-completeness 合併 + Book Phase 3）**

- **Stage 0：合併 `research/dashboard-completeness`**：Book主要checkout發現在一個從未push的本地branch上，12筆commit＋48個未commit檔案（chip_macro/ tracker產品化 + wantgoo_loop/ 情報迴圈子系統）。整理成2筆commit（`3ad1a85` chip_macro+wantgoo_loop子系統、`d05c536` 這次session早段遺留的fade訊號研究腳本），跑過完整測試（1556 passed）後 merge進main（`8c3130e`），push、刪除該本地branch。`config/job_registry.yaml`的合併衝突（main先前的`chip-macro-tracker`佔位條目 vs 該branch實際的`chip-macro-tracker-daily`完整條目）採用該branch版本，保留已知的`PANEL_DIR`寫死路徑bug提醒。
- **Stage 1：git結構收斂 + 意外發現的孤兒branch**：清掉`.claude/worktrees/hungry-yonath-aed334`worktree與對應branch。原訂直接刪除的孤兒branch `fix/launchd-documents-tcc-logpath`（07-28）經檢查發現：裡面的launchd log path修復本身早就在main的祖先鏈裡（非唯一），但另外藏了2筆從未進main的研究commit（`whale-quiet-accumulator-scan`／`whale-branch-position-momentum-2m` 兩個research.yaml主題 + 3支study腳本；4支外資賣壓/台積電相關study腳本）。跟用戶確認後cherry-pick進main（`483a515`、`071934e`），9239那部分結論因main後續有更完整的研究（rejected，理由更詳細）而捨棄不merge，其餘研究內容+腳本文件保留。跑過完整測試（1556 passed）後push、刪除孤兒branch（本地+remote）。
- **Stage 2（Book側）：code/state分離**：Book的`.env`(8K)/`data`(67GB，含15GB即時寫入的`stocks.db`)/`logs`(26M)/`CAFubon`(5.3M)用`cp -pR`（APFS clonefile）複製到`~/goldenstocks-data`，複製前確認無process正在寫入。驗證：檔案大小完全相符、`stocks.db`本身size+mtime byte-exact、`stock_daily_bars`列數相符(3,554,889)。`~/.zshrc`加上`export GOLDENSTOCKS_DATA_DIR=$HOME/goldenstocks-data`（Book本身無launchd job，純互動式session，跟mini的Aqua網域gap不同類）。**比照mini的謹慎模式：舊路徑`.env`/`data`/`logs`/`CAFubon`暫不刪除**，待穩定期驗證後（新路徑讀寫正常、無回退舊路徑跡象）才處理。

---

## 1. 決策摘要（現行）

### 1.1 機器

| 項目 | 決策 |
|------|------|
| 生產機 | **Mac mini**（無頭；**不裝 Cursor App**） |
| 研究機 | **MacBook**（唯一 IDE · 平常主入口） |
| 遠端遙控 | SSH `mac-mini`（同網／Tailscale）；手機用 SSH client 或 Screen Sharing |
| 路徑 | `/Users/jackm4/Documents/ETF/股票研究`（兩台相同） |
| 使用者 | `jackm4` |
| 網路 | mini **乙太網**；Book Wi‑Fi；同網 SSH 別名 `mac-mini`（見 §3） |
| Tailscale | **建議**（出國 SSH 備用）；日常同網 SSH 即可 |
| 雙機並跑 | **禁止**（Book 卸載全部 `com.jackm4.etf.*`） |
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
| 2b | `songshan-copytrade-poll` | 週一至五 09:25–09:40 每 5 分 | 跟單松山（凱基 `9217`）· 5d淨比95∩!mega + 25m nonfail · **預算制約10萬** · **live** |
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

### 1.5 遠端存取（Book 平常 · 出門/外地 · mini 常開）

> **cursor-agent-worker（Cursor "My Machines" KeepAlive worker）已於 2026-07 退役**：自
> 2026-07-24 啟用以來 log 無任何實際派工紀錄，判斷閒置未用（`CURSOR_AGENT_WORKER_ENABLED=0`
> + `launchctl disable`，安裝腳本 `install-cursor-agent-worker-launchd.sh` 已移除）。
> **取代方案**：Claude Code 雲端排程 routine `expert-pool-branch-health-check`（週一至五
> 21:00 台北 · Supabase MCP 唯讀健檢分點研究資料新鮮度 + Gmail 報告；與
> `daily-foreign-rotation-commentary` 職責切開，互不重疊）。

**目前的遠端存取方式（不再經 Cursor worker）**

| 場景 | 入口 | 工具跑在哪 |
|------|------|------------|
| 平常改碼／研究／回測 | Book Cursor / Claude Code IDE | **Book**（讀 replica，不碰 live ledger） |
| 出門查盤中 log／launchd／ledger 狀態 | SSH `mac-mini`（同網或 Tailscale） | **mini** |
| 純 GitHub 改碼／開 PR | 雲端 Agent（無 mini `data/`） | 雲 VM |
| 自動健檢分點資料新鮮度 | 雲端 routine `expert-pool-branch-health-check` | 雲端（唯讀 Supabase） |
| 裝機／狀態異常 | Screen Sharing／同網 SSH · mini CLI | mini |

**遠端 mini 操作原則**

- 機密／`data/`：SSH／scp／rsync，**不進 git、不貼進 LLM 聊天**。
- 看狀態優先 `scripts/launchd_status_dashboard.sh`、tail `logs/intraday/`；勿 `cat .env`／讀 `CAFubon/`。
- 出國 SSH：Tailscale（§5.4）。

---

## 2. 架構

```
MacBook（研究機 · 唯一 IDE）                Mac mini（無頭生產機）
─────────────────────────────────            ─────────────────────────────────
改 src/ config launchd 範本                    常開 · 乙太網 · 已登入
pytest / 小回測（讀 replica）                  WRITE SSOT：data/stocks.db
git push → GitHub  ←──────── git pull ───────── launchd：Order 袖 + radar + 夜間
SSH → 遙控部署與看 log                         live：C18 + Leading Dip + Songshan
本地 Agent（研究）                             + EP gate
無 live Order                                  Detach：RED 寄信 · 不送單
遠端：SSH mac-mini（同網／Tailscale）─────────► 操作／看 log 落在 mini
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
ORDER_SONGSHAN_COPYTRADE_BUDGET_TWD=100000  # 預算制約10萬台幣，2026-07-24 起（非固定股數）
ORDER_SONGSHAN_COPYTRADE_SUBMIT_EMAIL=1

# === 專家池漏斗閘門 live ===
RUN_EP_STAGED_GATE=1
ORDER_EP_STAGED_GATE_ENABLED=1
ORDER_EP_STAGED_GATE_DRY_RUN=0

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
| Tailscale | 出國穩定 SSH（仍建議裝） |
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
| 遠端 SSH 誤碰 live | 只讀狀態/log；禁止讀 `.env`／`CAFubon/`；改倉/送單一律在 mini 本機審慎執行 |

---

## 7. 相關

- `scripts/install-launchd.sh` — Order job SSOT  
- `scripts/install-order-launchd.sh` — `order-wake`  
- `scripts/launchd_status_dashboard.sh` — 盤前／盤中健檢  
- `.env.example` — 環境變數 SSOT  
- [docs/order-layer-prd.md](../../docs/order-layer-prd.md) — Order layer（下單層）  
- [docs/daily-operations.md](../../docs/daily-operations.md) — 日常操作  

**已刪**：`deploy/imac/`（iMac 轉向 stub · 2026-07-16）。
