# 系統總覽（新人先看這份）

> 這份文件回答「這整個系統有哪些機器/服務、怎麼分工、資料在哪、排程有哪些」。
> 深入細節請跳到對應文件，這裡只求 5 分鐘看懂全貌。

---

## 1. 一張圖看全貌

```
Book（MacBook Air · 唯一 IDE，研究/寫程式）
  ~/goldenstocks         程式碼（git checkout）
  ~/goldenstocks-data    .env/CAFubon/資料庫（不進git，機密）
  0 支 launchd 排程 —— Book 本身不跑任何自動化

        │ git push
        ▼
GitHub  goldenyears168-lab/goldenstocks（研究主專案）
        goldenyears168-lab/haoshi-quant-ops（網站前端）
        │ git pull
        ▼

Mac mini（無頭生產機，24小時常開）
  ~/goldenstocks         程式碼（跟 Book 同一份，各自獨立 git checkout）
  ~/goldenstocks-data    .env/CAFubon/資料庫（跟 Book 各自獨立，不同步）
  ~22 支 launchd 排程     全部見 config/job_registry.yaml
  下單能力：5 支 order job 全部暫停中（見 §3）

        │ 每日寫入
        ▼
Supabase（project: lzaomqzsiqudkojokevr，顯示名稱「好時股市研究」）
  ops.* schema           網站要顯示的東西（holdings/snapshots/live_ta/digests）
  stock_research schema  stock-intel 搜尋用的訊號表
  Edge Functions         stock-intel（搜尋API）、yahoo-quote（即時報價，網站呼叫）
                         ⚠ yahoo-cron/yahoo-daily-cron 已無排程呼叫，是死代碼（見§5）
        │
        ▼
網站  ~/goldenstocks-web（haoshi-quant-ops，Cloudflare Pages）
  私人 ops 後台，非公開展示站

雲端 Claude Code routine（4支，跟 mini 的 launchd 是兩個完全獨立的系統）
  見 §4
```

---

## 2. 三個路徑，各自的角色

| 路徑 | 內容 | 進 git？ | Book 有 | mini 有 |
|------|------|---------|---------|---------|
| `~/goldenstocks` | 程式碼 | ✓ | ✓ | ✓ |
| `~/goldenstocks-data` | `.env`／`CAFubon/`（券商憑證）／`data/stocks.db` | **✗ 機密**，`.gitignore` | ✓（研究用複本） | ✓（**生產正本**） |
| `~/goldenstocks-web` | 網站前端原始碼 | ✓（獨立 repo） | ✓ | — |

**Book 跟 mini 的 `data/stocks.db` 是兩份獨立資料庫，不會互相同步**——這是刻意設計，不是bug。程式碼用 `git push`/`git pull` 同步；資料/機密只用 SSH/scp/rsync，絕不進 git、絕不貼進聊天。

---

## 3. Order layer（下單層）現況

**目前無一支具備實際下單能力**——5 支 order-capable job（`rrg-c18acc-poll`／`leading-dip-poll`／`songshan-copytrade-poll`／`expert-pool-staged-gate`／`detach-gate`）都是三重保險同時鎖住：

1. `.env` 旗標本身安全（`DRY_RUN=1` 或 `ORDER_ENABLED=0`）
2. `launchctl disable`（重開機、重裝都不會復活）
3. `.env` 的 `ORDER_MASTER_ENABLED=0` 總開關

要恢復下單能力必須是明確、直接的指示，不會因為重跑安裝腳本或改其他設定而不小心復活。

---

## 4. 排程總清單

| 系統 | 支數 | SSOT |
|------|------|------|
| mini launchd | ~22 | `config/job_registry.yaml` |
| Book launchd | 0 | 設計上就是這樣 |
| Claude Code 雲端 routine | 4 | `config/job_registry.yaml` 的 `cloud_routines` 區塊 |
| GitHub Actions | 1（`test.yml`） | 只在 push/PR 時跑測試，不是定時排程 |
| Supabase pg_cron | 0（現行） | 見 §5 |

4支雲端routine跟mini的22支launchd是**兩個完全獨立、互不知道對方存在的系統**：mini的排程靠本機launchd時鐘，雲端routine靠Claude Code自己的排程機制，兩邊都只讀Supabase/Gmail，不會互相觸發。

---

## 5. 已知的技術債（暫不處理，寫下來避免以後忘記）

- **Supabase Edge Function `yahoo-cron`／`yahoo-daily-cron`**：2026-07-23之前有 pg_cron 排程呼叫，之後排程被移除，兩個function還在線上但沒人叫用了——是死代碼，可以考慮清掉但還沒動。
- **Supabase Edge Function `simplybook-proxy`**：內容是攝影工作室（好時系統，另一個不相關的業務）的預約系統後端，混進了這個股市研究專案裡，不影響功能但歸屬不對。
- **`docs/` 資料夾約28個文件**，部分是研究過程留下的階段性筆記，沒有嚴格的閱讀順序——遇到疑問優先看 `docs/PRD.md`、`docs/daily-operations.md`、`config/job_registry.yaml`。

---

## 6. 我是新人，接下來要看什麼？

| 想知道 | 看這份 |
|--------|--------|
| 完整產品範圍、策略清單 | [PRD.md](./PRD.md) |
| 研究 pipeline（Facts/Regime/Research分層） | [architecture.md](./architecture.md) |
| 每日排程細節、Supabase sync SOP | [daily-operations.md](./daily-operations.md) |
| Order layer 完整規格 | [order-layer-prd.md](./order-layer-prd.md) |
| mini 現在裝了哪些job、狀態 | `config/job_registry.yaml` |
| 怎麼 SSH 進 mini | `~/.ssh/config` 的 `Host mac-mini` |
