# supabase/site · 作者維護說明

本目錄為 `site_content` 的 SSOT。**README 不推送 Supabase**（sync 腳本略過 `README.md`）。

**Supabase 查表**：Dashboard 請選 schema **`stock_research`**（`daily_briefs` · `site_content` · `strategy_performance_yearly` · `stock_daily_highlight` · `daily_highlight_alert`）。`public` 無研究表；官網預約仍在 `public.booking_logs` 等。

## 連結規則（僅三類）

| 類型 | 寫法（MD） | Readdy 實作 |
|------|------------|-------------|
| **靜態頁** | `[策略目錄](strategy_catalog)` | slug 路由 |
| **日報** | `[最新市場環境](/)` · 歷史 `[某日市場環境](/briefs/2026-06-18/regime)` | 日報首頁 `/` 或 `daily_briefs` |
| **同層錨點** | `[怎麼讀這張表](#怎麼讀這張表)` | 同一頁 scroll |
| **跨頁錨點** | `[績效對照](strategy_catalog#績效對照)` | slug + hash |

日報 type：`regime` · `etf` · `vcp`。對外連結優先 **`/`（最新日報）** 或 **`/briefs`（選日）**；績效窗口內的示例日期可寫在表格註腳，勿硬編在首頁導覽。

勿用：repo 路徑 · `page_id=` · 相對 `.md` 路徑 · 「日期選擇器」純文字（改日報連結）。

## AUTO 區塊

| marker | 檔案 |
|--------|------|
| `AUTO:lxh-matrix` | `research/research_case_copytrade.md` |
| `AUTO:vcp-sweep-top25` | `research/research_case_vcp_funnel.md` |
| `AUTO:rrg-breadth` | `research/research_case_rrg_mono.md` |

刷新 AUTO 區塊或 **UI 文案 SSOT**（`lens_ui_copy.py` · `supabase/site/`）後推送：

```bash
# 文案一鍵：site_content + highlight narrative（建議）
./scripts/resync_readdy_ui_copy.sh
./scripts/resync_readdy_ui_copy.sh --latest          # 僅最近 1 交易日
./scripts/resync_readdy_ui_copy.sh --site-only       # 只推靜態頁

# 本機 supabase/site/ 存在時
PYTHONPATH=src .venv/bin/python scripts/sync_site_content_to_supabase.py

# 僅 git HEAD 快照（無本機 site/ 目錄時）
PYTHONPATH=src .venv/bin/python scripts/push_site_content_md.py
PYTHONPATH=src .venv/bin/python scripts/push_site_content_md.py --page research_case_copytrade
```

詳見 [readdy-regime-strategy-lineage.md §7.4](../../docs/readdy-regime-strategy-lineage.md)。

## page_id 一覽

| page_id | 用途 |
|---------|------|
| `project_home` | 專案首頁 · 對外敘事 · 六格 KPI 摘要 |
| `daily_home` | **Readdy `/` 首屏規格** · KPI 解析 · 三卡 · RRG timeline 嵌入 |
| `layer_facts` … `layer_website` | 六層 tab |
| `strategy_catalog` | 策略目錄 |
| `strategy_*` | 策略獨立頁 |
| `research_case_*` | 研究示範案例 |

Readdy 消費者 nav：**今日**（`/` ← `daily_home` 契約）· **日報** · **方法論**（策略目錄 + 六層）。
