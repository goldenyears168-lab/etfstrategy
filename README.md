# ETF 持股研究 · 投資決策引擎

本地 **SQLite**（`data/stocks.db`）+ 方案 C 四排程（①②③ ingest + ④ 策略回顧）。文件見 [docs/README.md](docs/README.md)、[docs/PRD.md](docs/PRD.md)。

> **免責**：本專案產出僅供個人研究與內部決策輔助，**不構成投資建議或要約**。規則評分、觀察名單與報告內容不保證未來結果；實際下單請自行判斷並自負風險。

## 5 分鐘上手

1. **環境**（專案根目錄）

```bash
cd "/path/to/股票研究"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 編輯 .env：至少填入 TEJ_API_KEY（早盤日線）；收盤 Score 建議 RUN_SCORE_ENGINE=1
```

2. **第一次跑收盤流程**（會打 API 寫入 `data/stocks.db`，並在終端印研究報告）

```bash
# 或 Finder 雙擊
scripts/1630收盤雷達.command
```

3. **對照範例產出**（不需先懂全部 code）

| 檔案 | 內容 |
|------|------|
| `logs/daily_sync_YYYYMMDD.log` | 同步步驟、耗時、`OK`/`WARN` |
| `reports/YYYYMMDD_research_context.json` | 規則引擎結構化結果（`as_of_date`、`score_version`） |
| `reports/YYYYMMDD_evening_brief.md` | 收盤人類主檔（摘要、研究表、查證、Checklist） |
| `reports/YYYYMMDD_research_context.json` | 給外部 LLM 的結構化資料（規則欄位為權威） |
| `reports/YYYYMMDD_prompt_evening_full.txt` | 給外部 LLM 的合併提示詞（含 §3 待查新聞聯網查證 · `RUN_EXPORT_AI_BUNDLE=1`） |
| `reports/YYYYMMDD_evening_brief.md` | 營運摘要（若已產生） |

收盤跑完後，`reports/YYYYMMDD_*` 會在本機產生（已 gitignore）。

4. **確認 DB 健康**（可選）

```bash
export PYTHONPATH=src
.venv/bin/python src/report_summary.py --mode evening-health
```

詳細 SOP：[docs/daily-operations.md](docs/daily-operations.md)。

## 目錄

| 路徑 | 內容 |
|------|------|
| `src/` | Python：同步、行為訊號、DB（`stock_db.py`） |
| `scripts/` | `daily_sync.sh`、`weekly_sync.sh`、`.command` 入口 |
| `docs/` | `PRD.md`（規格全集）、`daily-operations.md`（每日 SOP） |
| `data/` | `stocks.db`（gitignore） |
| `logs/` | 同步 log（gitignore） |
| `tests/` | 單元測試 |
| `.cursor/skills/` | Agent 工作流（同步、排程、Intent） |
| `reports/` | 每日營運產物（gitignore；brief、checklist、signal_review 等） |

## 日常操作

```bash
cd "/path/to/股票研究"
# 或雙擊 scripts/ 內 .command
scripts/0830執行評估.command  # ① 08:30
scripts/0845試撮重算.command  # ①b 08:45（測試）
scripts/0850開盤確認.command  # ①c 08:50
scripts/0905盤中預覽.command  # ①d 09:05 Yahoo 自動查價
scripts/1630收盤雷達.command  # ② 16:30
scripts/2000週日補庫.command  # ③ 週日 20:00
scripts/策略回顧.command    # ④ 隨時（§0 Flow + Paper 10 萬）
```

手動單支腳本（需在專案根目錄）：

```bash
export PYTHONPATH=src
.venv/bin/python src/sync_etf_holdings.py --etf-codes 00981A --changes --intent
```

## 測試

```bash
export PYTHONPATH=src
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

## Sanity check（資料健康）

排程跑完或懷疑 DB 異常時，只讀 `stocks.db`、不打 API：

```bash
export PYTHONPATH=src
# 早盤：tech_risk、pm_watchlist、成分股 K 線最新日
.venv/bin/python src/report_summary.py --mode morning
# 收盤：持股 snapshot、評分、法人、催化事件筆數
.venv/bin/python src/report_summary.py --mode evening-health
# 週日補庫後：Beta / 基本面覆蓋
.venv/bin/python src/report_summary.py --mode weekly
```

預期：各表「最新交易日」應接近最近營業日；`investment_scores` 在收盤且 `RUN_SCORE_ENGINE=1` 後應有列。
