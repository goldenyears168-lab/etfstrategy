# reports/publish/ · 網站層 VFP

**Website layer SSOT** — 對外發布區。本地 `web/` dev 讀此目錄；17:00 upsert 至 Supabase `stock_research.daily_briefs`。

| 路徑 | 產品層 | Supabase `brief_type` |
|------|--------|------------------------|
| `facts/etf-daily/daily_brief.md` | Facts · 事實層 | `etf_daily` |
| `facts/etf-daily/YYYYMMDD.md` | Facts 歷史封存 | `etf_daily` |
| `regime/daily_brief.md` · `.embed.html` | Regime · 環境層（最新） | `regime_daily` |
| `regime/snapshots/YYYYMMDD/` | Regime 歷史封存 | `regime_daily` |
| `research/vcp_funnel_specs/YYYYMMDD.md` | Research · 研究層 | `vcp_funnel_specs` |
| `strategy/catalog.md` | Strategy · 策略層摘要 | （尚未 sync） |

## 寫入

Pipeline 產出時自動 mirror（`src/website_publish.py`）：

- `etf_daily_report.py --write-reports`
- `regime_daily_brief.py --write-reports`
- `vcp_funnel_specs_daily.py`

一次性從 legacy `reports/daily/` 回填：

```bash
python scripts/mirror_to_publish.py
```

## 同步 Supabase（手動 · 排程已移除）

```bash
RUN_SUPABASE_RESEARCH_SYNC=1 python scripts/backfill_supabase_research.py --days 14
```

路徑常數：`src/website_publish.py` · sync catalog：`src/supabase_research_sync.py`
