# 每日營運速查

> **正文以 [PRD.md](./PRD.md) 為準**（v0.3 · **方案 C**）。本檔僅供快速連結。

## 方案 C：四個排程（預設）

| # | 名稱 | slug | 建議時間 | 入口 |
|---|------|------|----------|------|
| ① | **執行評估**（原早盤風險哨） | `execution-eval` | 週一至五 08:30 | `scripts/0830執行評估.command` |
| ①b | **試撮重算**（測試用） | `auction` | 08:45–08:59 | `scripts/0845試撮重算.command` |
| ①c | **開盤確認** | `approve` | 08:50–08:55 | `scripts/0850開盤確認.command` |
| ①d | **盤中預覽**（測試用） | `intraday` | 09:05+ | `scripts/0905盤中預覽.command` |
| ② | **收盤持股雷達** | `evening-holdings` | 週一至五 16:30 | `scripts/1630收盤雷達.command` |
| ③ | **週日深度補庫** | `weekly-deep` | 週日 20:00 | `scripts/2000週日補庫.command` |
| ④ | **策略回顧** | `signal-review` | 隨時 | `scripts/策略回顧.command` |

> ④ 規格：[signal-review-PRD.md](./signal-review-PRD.md)（Paper 10 萬每日全換 · 只讀 DB）

---

## 兩段營運節奏（固定 SOP）

### ① 執行評估（08:30 · 規格 [execution-eval-PRD.md](./execution-eval-PRD.md)）

1. 雙擊 `0830執行評估.command`（或 launchd 定時）
2. 終端依序閱讀：
   - **tech_risk**：TSM ADR、半導體、台指 gap、電子期
   - **開盤前執行摘要**：前日 `pm_watchlist`（突破 / 觀察 / 回避）
   - **今日建議掛單（E0）**：`✓ 建議掛單` / `✗ 風控略過`
   - **開盤風控 Checklist**：勾選 `[ ]` 項目
3. **08:45–08:59**：試撮後雙擊 `0845試撮重算.command`，或手動 `execution_eval.py`：
   - 手動：`PYTHONPATH=src python3 src/execution_eval.py --mode auction --prices 2330=2310 --persist`
   - FinMind：`... --mode auction --price-source auto --persist`（需 `FINMIND_TOKEN`；可再用 `--prices` 覆寫單檔）
4. **人工風控**（必守）：
   - TSM ADR < -2% → 科技新倉降一檔
   - `pm_bucket=回避` 或 `entry_signal=暫不進場` → 不追
   - 假共識 / 外資賣超背離 → 降優先
   - Gap 過大 → 縮 size 或改限價（現階段人工；E0.2 規則化）
5. **08:50–08:55**：雙擊 `0850開盤確認.command` 核准掛單
6. **09:05+**（可選）：雙擊 `0905盤中預覽.command`（Yahoo 1m 自動查價 · `--preview` · 不寫 DB）

### ② 收盤持股雷達（16:30）

1. 雙擊 `1630收盤雷達.command`
2. 終端 digest 摘要後，開啟 `reports/YYYYMMDD_evening_brief.md` 勾 Checklist
3. `RUN_EXPORT_AI_BUNDLE=1` 時貼 `reports/*_prompt_evening_full.txt` 至**有聯網能力**的外部 LLM（含 §3 待查新聞查證；**不得覆寫** watchlist / 權重）

### ③ 週日深度補庫（20:00）

1. 雙擊 `2000週日補庫.command`
2. 補 **Beta、基本面、成分股 90 日 batch**（餵預期差 / 基本面子分）
3. 平日 `RUN_STOCK_MARKET_SYNC=1` 只做 incremental；週日做深度補底

### ④ 策略回顧（隨時）

1. 雙擊 `策略回顧.command`（預設 `--lookback-trading-days 7 --lookback-event-days 20`）
2. 讀 `reports/YYYYMMDD_signal_review.md`：
   - **§0 ETF Flow Attribution**（只讀 `flow_events`；Boss Gate H+3/H+5、Coverage）
   - 分桶 IC、Paper 10 萬每日全換損益
3. 樣本不足或 §0 Coverage Available=0 時報告會標「不建議改 rule」
4. `daily_sync` 結尾 **Flow 歸因自檢**（`print_flow_attribution_readiness`）會提示可否跑 §0

**§0 前置**：② 收盤 `--intent` 已落地 `flow_events`，且成分股 K 線（`RUN_STOCK_MARKET_SYNC=1`）有 **event 後至少 1 個交易日**。

---

## 催化 / 新聞政策

| 方式 | 設定 | 說明 |
|------|------|------|
| **預設（建議）** | `USE_MANUAL_EVENTS=0` | 不用 `manual_events.json`；待查清單在 JSON + evening_brief；**聯網 LLM 用 prompt §3 查證** |
| Perplexity 拉新聞 | `RUN_NEWS_SYNC=1` | 可選；費用與雜訊較高 |
| Perplexity 查證 | `RUN_PERPLEXITY_VERIFY=1` | 重大事件日才開 |

查證連結範例：`https://tw.stock.yahoo.com/quote/2330.TW/news`

---

## `.env` 收盤建議（無衝突版）

```bash
ENABLE_FINMIND_SIGNAL=1      # 可選：ETF 三大法人（403 時關閉）
RUN_STOCK_MARKET_SYNC=1      # 成分股 K+法人；§0 Flow 歸因必要
RUN_SCORE_ENGINE=1
RUN_EXPORT_AI_BUNDLE=1
USE_MANUAL_EVENTS=0
RUN_CATALYST_ENGINE=0
RUN_NEWS_SYNC=0
RUN_PERPLEXITY_SUMMARY=0
RUN_PERPLEXITY_VERIFY=0
ENTRY_OVEREXTENDED_ABS_MIN=13
ENTRY_OVEREXTENDED_REL_PCT=78
```

---

## 產出檔案（收盤雷達 · 預設 4 份）

| 檔案 | 用途 |
|------|------|
| `reports/YYYYMMDD_evening_brief.md` | **人類唯一主檔**（摘要、研究表、查證、Checklist） |
| `reports/YYYYMMDD_research_context.json` | LLM / API（`RUN_EXPORT_AI_BUNDLE=1`） |
| `reports/YYYYMMDD_prompt_evening_full.txt` | 外部 LLM 提示詞（含 §3 待查新聞聯網查證） |
| `logs/daily_sync_YYYYMMDD.log` | 同步除錯 |

| 檔案 | 時點 |
|------|------|
| `reports/YYYYMMDD_signal_review.md` | ④ 策略回顧 |

---

| 想查什麼 | PRD 章節 |
|----------|----------|
| 架構圖、Phase 對照 | **§5.2** |
| 改造 checklist | **§22** |
| 五層架構 | [architecture.md](./architecture.md) |
