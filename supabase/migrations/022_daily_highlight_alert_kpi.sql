-- Lens daily alert · view-ready KPI aggregates for Readdy (no client-side count)
-- Builder: src/lens_alert_digest.py · sync: src/supabase_lens_sync.py

alter table stock_research.daily_highlight_alert
  add column if not exists total_count int not null default 0,
  add column if not exists consensus_add_count int not null default 0;

comment on column stock_research.daily_highlight_alert.total_count is
  '監控清單內標的數 · publish-time aggregate';
comment on column stock_research.daily_highlight_alert.consensus_add_count is
  'ETF 共識加碼檔數 · publish-time aggregate';
