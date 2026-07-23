# scripts/tools · infra / 手動工具

非 daily_sync 主線 · 非 `run_*` pipeline runner。按需手動執行。

| Script | 用途 |
|--------|------|
| `backfill_daily_reports.py` | 歷史 daily brief 補寫 |
| `backfill_historical_constituents.py` | 成分股歷史 backfill |
| `backfill_kbar_5m_from_1m.py` | 1m → 5m K 聚合 backfill |
| `backfill_ix0001_finmind_5m.py` | FinMind 5s 指數 → IX0001 5m（reconcile Yahoo） |
| `backfill_phase2_is_kbar.py` | Phase2 IS kbar backfill |
| `backfill_stock_daily_lens.py` | stock_daily_lens → Supabase |
| `backfill_supabase_research.py` | research daily_briefs backfill |
| `backfill_vcp_funnel_screen.py` | VCP funnel screen 歷史 |
| `analyze_extension_peak_features.py` | extension peak 特徵分析 |
| `analyze_extension_peak_features_2c.py` | extension peak 2c 分析 |
| `calibrate_extension_probs.py` | extension 機率校準 |
| `calibrate_overnight_gap_seg.py` | overnight gap 分段校準 |
| `generate_readdy_ui_copy.py` | uiCopy.generated.ts 生成 |
| `organize_research_html.py` | research HTML 整理 |
| `push_site_content_md.py` | git HEAD MD → Supabase site_content |
| `sync_site_content_to_supabase.py` | 本機 supabase/site → site_content |
| `sync_us_overnight_futures.py` | 美期 overnight 資料 sync |
| `validate_tw100_data.py` | TW100 資料驗證 |
| `write_copytrade_slot_summary.py` | copytrade slot 摘要 |
| `render_order_layer_flowchart.py` | Order layer 單頁流程圖 PDF |
| `render_order_layer_teach_deck.py` | Order layer 多頁教學簡報 PDF |

**仍留 `scripts/` 根目錄的 infra**（daily_sync / launchd 引用）：

- `backfill_c18acc_kbar.py` · `sync_research_to_supabase.py` · `sync_strategy_performance.py`
- `supabase_health_check.py` · `import_etfedge_holdings.py`

**Research HTML render**（showcase / teaching deck）→ `scripts/research/archive/render_*.py`
