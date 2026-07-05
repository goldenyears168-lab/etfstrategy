# supabase/site · Git authoring mirror

**Runtime SSOT（公開站）**：Supabase `stock_research.site_content` · Readdy 只讀 DB。

**本目錄角色**：git 版 **authoring mirror** — 在此編輯 Markdown + frontmatter，推送後覆寫 Supabase 對應列。勿在 Dashboard 手改後不同步回 git（會漂移）。

## Publish 流程

```bash
# 推 site_content（本目錄存在時）
./scripts/resync_readdy_ui_copy.sh --site-only

# 或僅 site_content
PYTHONPATH=src .venv/bin/python scripts/sync_site_content_to_supabase.py
```

需 `.env`：`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`。

**Supabase 查表**：Dashboard → schema **`stock_research`**（`site_content` · `daily_briefs` · `strategy_performance_yearly` · `stock_daily_highlight`）。

## 對外 IA（canonical）

| Nav | 路由 | SSOT 表 |
|-----|------|---------|
| **今日** | `/` | `daily_briefs` + `regime_daily` snapshot |
| **日報** | `/briefs` | `daily_briefs` |
| **策略目錄** | `/strategies` | `site_content`（`layer_id=strategy` registry）+ `strategy_performance_yearly` |
| **关于** | `/about` | `site_content`（`project_home` · `layer_*` 方法論附錄） |

**勿作頂 nav**：獨立 Research 層 · `layer_research` 案例索引 · `/pages/strategy_catalog`（301 → `/strategies#…`）。

詳細 IA：[readdy-regime-strategy-lineage.md §1.3](../../docs/readdy-regime-strategy-lineage.md)。

## 連結規則（MD → React）

| 類型 | 寫法（MD） | 前端路由 |
|------|------------|----------|
| 策略目錄長文 | `[策略目錄](strategy_catalog)` | `/strategies` · `/strategies#…` |
| 凍結規格 | `[…](strategy_00981a_l1h9)` | `/strategies/:strategy_id` |
| 採納報告 | **勿**直連 `research_case_*` | 讀者經策略頁 **採納報告** tab（`/strategies/:id/lineage`） |
| 日報 | `[最新](/)` · `[某日](/briefs/2026-06-18/regime)` | 日報路由 |

## AUTO 區塊

| marker | page_id |
|--------|---------|
| `AUTO:lxh-matrix` | `research_case_copytrade` |
| `AUTO:vcp-sweep-top25` | `research_case_vcp_funnel` |
| `AUTO:rrg-breadth` | `research_case_rrg_mono` |

刷新 AUTO 表後：`--site-only` 重推，或 patch Supabase `content_md` 並回寫 git。
