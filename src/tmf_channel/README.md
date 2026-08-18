# `src/tmf_channel/` — TMF 微型台指通道策略・凍結執行套件

TMF（微型臺指期）micro-channel 策略的**凍結套件**：live simulate 的 SSOT。
launchd KeepAlive worker 常駐重用 Fubon session，窗內每 20 秒對帳一次（實測約 0.19s/輪）。
研究 lab（`reports/research/channel_lab/`）只剩 shim 與研究腳本，**不在 live import 鏈上**。

## 執行路徑

```
launchd KeepAlive (com.jackm4.goldenstocks.tmf-channel-poll)
  │
  ▼
tmf-channel-poll launcher（lockdir 防雙跑 · 單一執行路徑）
  │
  ▼
scripts/order/run_tmf_channel_worker.py（.venv-fubon）
  │
  ▼
tmf_channel.worker_loop.run_forever()
  │   交易窗內每 20s 一輪；窗外 60s 空轉（ORDER_TMF_CHANNEL_WORKER_INTERVAL/_IDLE）
  ▼
tmf_channel.session_pool（Fubon 登入一次、跨輪重用；異常時 reset 重連）
  │
  ▼
order.tmf_channel_order.reconcile_once()   ← 每輪對帳入口
  │
  ▼
tmf_channel.causal_engine → desired-state（這一根 bar「應該」掛什麼單）
  │
  ▼
order.fubon_futopt_orders place / cancel
      實彈需 .env 四鎖全開：ORDER_MASTER_ENABLED=1 ·
      ORDER_TMF_CHANNEL_ENABLED=1 · ORDER_TMF_CHANNEL_DRY_RUN=0 ·
      ORDER_TMF_CHANNEL_AUTO_SUBMIT=1；缺一即 dry-run（fail-closed）
```

## 模組表

| 模組 | 職責 |
|------|------|
| `causal_engine.py` | Simulate SSOT（約 3000 行，causal O-anchor）。Order 與 research 共用同一份；**勿再 fork** |
| `engine.py` | Public API 門面（re-export `simulate` / `summarize` / loaders）。外部**只該** import 這裡，不要直接 import `causal_engine` |
| `harness.py` | 研究**唯一入口**（`run_days` / `summarize_days`）。強制 live `PAPER_RECIPE`；`assert_live_recipe` 擋手抄過期 recipe |
| `worker_loop.py` | 常駐 worker 主迴圈：窗內 20s / 窗外 60s，SIGTERM 優雅停止，異常不退出（KeepAlive 只在 crash 時重啟） |
| `session_pool.py` | Fubon session 池：登入一次跨輪重用、`init_realtime` 每 session 至多一次、異常 reset |
| `nq_gate.py` | NQ 隔夜 drift gate → cell.bias（L/S/none）。**fail-safe**：任何載入/評估錯誤回 `None`，絕不 raise 進下單層 |
| `nq_signal.py` | NQ/ES 隔夜訊號的凍結副本（逐字移植自 lab R5 腳本；數值一致性由測試釘住）。資料檔仍經 lab symlink（見不變式） |
| `blotter.py` | Fubon live-book session blotter：FIFO 配對 + realized PnL（paper UI 共用；1 pt = NT$10） |
| `aux_cache.py` | VIX / gap-bias 輔助載入的 TTL cache（避免每輪全表掃描） |
| `cache_store.py` | TX 1m bars 的 day-lazy 快取（SQLite `bars.sqlite` SSOT + JSON fallback） |
| `desired_cache.py` | Desired-state fingerprint 去重（memory + disk；同一根 bar 內 worker 重啟不重算） |
| `trade_journal.py` | 持倉期間每輪的軌跡＋當下所有作用中因子（append-only JSONL）；並提供「符合預期」的兩層量化：單筆 z（訊噪比 0.058，**只記錄不觸發**）與滾動 z（n=20 · SE≈10.6 點，破 2σ 的動作是**停止開新倉**，不是砍現有部位）。對下單路徑唯讀且 fail-safe |
| `tick_index.py` | 從 FinMind 逐筆重建 tick 索引（含每筆量與秒級時戳），供 `causal_engine` 的 `tick_native` 回放與 `fill_model` 使用。⚠️ 必須濾掉日曆價差列（見模組說明） |
| `legacy_helpers.py` | 舊 `jack_channel_v5` 的最小 helper，讓 live import 鏈不必碰 lab 的 sys.path |

## 設定與 recipe 流向

```
config/order.yaml（sleeve 規格）
  → .env ORDER_TMF_CHANNEL_*（env 蓋 yaml；實彈旗標只在 mini .env）
  → src/order/tmf_channel_config.py  → PAPER_RECIPE（live recipe 組裝）
  → src/order/tmf_channel_pv16_book.py → 16 格 cell book
       （day|night × PV8；SPECIALIZED_PATCHES + CELL_TUNE_V2 疊加；RECIPE_VERSION）
```

## 不變式（invariants）

1. **單一執行路徑**：只有 launchd `tmf-channel-poll` 一條路。**禁止** nohup / 手動 daemon 雙跑。
2. **Fail-closed 預設**：plist / launcher template 一律 dry-run；實彈只由 mini `.env` 四鎖開啟。
3. **本套件不得 import `reports/`**：程式依賴已歸零。已知 TODO：`nq_signal` 的資料檔路徑仍指向 lab 下的 symlink（實體在 `${GOLDENSTOCKS_DATA_DIR}/cache/tmf_channel/`）。
4. **Broker 持倉 authoritative**：對帳以富邦回報為準，本地 ledger 只是紀錄。
5. `causal_engine` 是唯一 simulate 實作；研究要變體走 recipe 參數，不 fork 引擎。

## 常見任務

| 任務 | 做法 |
|------|------|
| 改 cell 參數 | 改 `src/order/tmf_channel_pv16_book.py` + 對應測試 `tests/test_tmf_channel_pv16_book.py` |
| 跑研究回測 | `from tmf_channel.harness import run_days, summarize_days`（禁手抄 recipe；一律 `PAPER_RECIPE`） |
| 除錯單輪 | `.venv-fubon/bin/python scripts/order/run_tmf_channel_poll.py --json` |
| 部署新碼 | `scripts/order/tmf_cutover.sh`（preflight import → kickstart → 等首輪對帳；`--dry` 只 preflight） |
| 看 live log | `${GOLDENSTOCKS_DATA_DIR}/logs/intraday/tmf_channel_live_YYYYMMDD.log` |
| 看每筆交易的圖形與因子 | `PYTHONPATH=src .venv/bin/python scripts/research/tmf_trade_journal_report.py --days YYYY-MM-DD` |

## 測試

```bash
.venv/bin/pytest \
  tests/test_tmf_channel_package.py \      # 架構邊界（不得 import reports/ 等）
  tests/test_tmf_worker_loop.py \          # worker 迴圈 / session pool
  tests/test_tmf_nq_gate.py \              # NQ gate 數值一致性（釘 lab 移植）
  tests/test_tmf_channel_pv16_book.py \    # 16 格 cell book
  tests/test_tmf_channel_order.py \        # reconcile / 下單層
  tests/test_tmf_fubon_live_book.py -q     # blotter FIFO 配對
```
