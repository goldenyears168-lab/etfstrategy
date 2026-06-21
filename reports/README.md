# reports/ 目錄

| 子目錄 | 用途 |
|--------|------|
| **`publish/`** | **Website layer VFP** · 對外發布區（web dev + Supabase sync） |
| **`daily/`** | 排程產物（Facts · Regime · launchd brief 根檔） |
| **`research/`** | 回測 JSON · 廣度 HTML · copytrade 深度研究 |
| **`samples/`** | 可提交的格式範例（版控） |

根目錄僅保留本 README；**勿**再在 `reports/` 根寫入新檔。路徑常數：`src/report_paths.py`

## daily/ — 現行產物

| 路徑 | 策略 / 用途 |
|------|-------------|
| `etf-daily/daily_brief.md` | **Facts** · `etf-daily` |
| `regime/daily_brief.md` | **Regime** · `regime-daily` |
| `{date}_etf_daily.md` | Facts  dated 副本 |
| `vcp_funnel_specs_daily_brief.md` | VCP Pivot Gate + Coil Close（launchd 13:00） |
| `vcp_pivot_gate_daily_brief.md` · `vcp_coil_close_daily_brief.md` | VCP 各 spec |
| `rrg_mono_daily.md` · `rrg_mono_intraday_watch.md` | RRG mono（launchd 13:00 / 16:40） |

**閱讀順序**：`etf-daily/daily_brief.md` → `regime/daily_brief.md` → launchd 各軌。

**已清除**：舊 per-track 子目錄（`p6-tier-flow` · `research-os` · `00981a-copytrade-l1` 等）與 `track_evaluation_summary.md`。

## research/ — 深度研究

| 資料夾 | 內容 |
|--------|------|
| `00981a-copytrade/` | L1H9 基準、H1、leg 歸因、資金週期、RRG 稽核 |
| `rrg/` | RRG universe 軌跡、rotation 回測、mono × breadth |
| `breadth/` | 200MA 廣度 HTML、momentum 結構 |
| `vcp/` | VCP benchmark、春哥 L4 校準 |
| `minervini-sepa-basket/` | broad_momentum 回測 JSON |
| `_archive/` | 已停損研究摘要 |
| `strategy_hub.html` | 多軌 Dashboard（手動 render） |

## _archive/ 封存索引

| 摘要 | 原報告 | 資料來源 |
|------|--------|----------|
| `00981a-filter-studies.md` | §10 filter #2–#10 | `copytrade_*_filter_compare` |
| `etf-behavior-studies.md` | v8/v9 行為預測 | 方法論 §10 #5 |
| `inst-flow-studies.md` | 法人 flow 回測 | `run_inst_flow_backtest.py` |
| `exploratory-studies.md` | S04 / FinPilot / tw_stocker 對照 | 一次性腳本 |

決策紀錄：[docs/00981a-retired-research.md](../docs/00981a-retired-research.md)
