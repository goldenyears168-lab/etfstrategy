# 股票研究 · 台股量化交易 Research OS

**當前版本**：v2.1（2026-07-24）

台股量化交易研究系統：本地 **SQLite**（`data/stocks.db`）+ market data ingest + **多軌並行 alpha 策略**（RRG 動能輪動、VCP 型態篩選、Minervini SEPA、00981A 跟單 copytrade 等）+ **Facts / Regime daily** 每日診斷 + Mac mini 自動下單執行層。

> **專案起源**：ETF 持股追蹤（Phase 0），現行 **ETF 持股變化只是其中一項資料來源與訊號**（`etf-daily` Facts 層 + `00981a-l1h9` 跟單訊號），並非整個系統的核心；核心是個股層級的多軌策略研究。

> **免責**：產出僅供個人研究，不構成投資建議。所有數據與報告皆在本地，不進行公開展示。

> ⚠️ **重大變更歷史**：
> - **2026-07-24**: Songshan copytrade 改為預算制（約 10 萬零股）
> - **2026-07-23**: 公開站 Readdy 退役（移至私人 ops 後台 `haoshi-quant-ops`）
> - **2026-07-16**: ABC Order 下單軌退役

---

## 從這裡開始

| 想知道 | 看這份 |
|--------|--------|
| 完整產品範圍、資料層、策略清單、非目標 | [docs/PRD.md](docs/PRD.md) |
| 系統架構、分層、公開站 IA | [docs/architecture.md](docs/architecture.md) |
| 每日排程、launchd、Supabase sync SOP | [docs/daily-operations.md](docs/daily-operations.md) |
| 術語規範（中英對照 SSOT） | [docs/terminology.md](docs/terminology.md) |
| `src/` 模組分層（L0–L5） | [docs/src-map.md](docs/src-map.md) |
| LLM / agent 任務導航 | [docs/agent-brief.md](docs/agent-brief.md) |
| 所有文件完整索引 | [docs/README.md](docs/README.md) |

---

## Quick start

```bash
cd ~/goldenstocks
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 編輯 TEJ_API_KEY、FINMIND_TOKEN
scripts/1630收盤雷達.command
```

---

## 每日先看這兩份

| 檔案 | 內容 |
|------|------|
| [`reports/daily/etf-daily/daily_brief.md`](reports/daily/etf-daily/daily_brief.md) | **Facts** · 各檔 ETF 持股變化 |
| [`reports/daily/regime/daily_brief.md`](reports/daily/regime/daily_brief.md) | **Regime** · 四軸市場環境 |

其餘策略軌（VCP、RRG mono、Minervini SEPA、buy/sell signal radar…）的 daily brief 清單與排程時間，見 [docs/PRD.md](docs/PRD.md) 與 [docs/daily-operations.md](docs/daily-operations.md)。

---

## 研究收官（Phase 1–6）

觀盤儀表板 16 維完整性研究的**收斂總表 / executive summary**：
[`reports/research/dashboard-completeness/STATE_OF_DASHBOARD.md`](reports/research/dashboard-completeness/STATE_OF_DASHBOARD.md)
— 真增益總帳（tech-gated champion 系統 C · VIX gate · rev 家族含可交易性裁決）、全部證偽維度清單、殘餘風險、STOP/CONTINUE 建議。可部署系統規格見同目錄 [`DEPLOYABLE_SYSTEM.md`](reports/research/dashboard-completeness/DEPLOYABLE_SYSTEM.md)。
