-- Today highlight · featured ranks + badge SSOT for Readdy
-- Builder: src/stock_daily_lens.py · sync: src/supabase_lens_sync.py

alter table stock_research.stock_daily_highlight
  add column if not exists featured_rank int,
  add column if not exists home_preview_rank int,
  add column if not exists badges_json jsonb not null default '[]'::jsonb;

comment on column stock_research.stock_daily_highlight.featured_rank is
  '日報今日亮點精選排序 1–10 · null = 非精選';
comment on column stock_research.stock_daily_highlight.home_preview_rank is
  '首頁預覽排序 1–6 · null = 非預覽';
comment on column stock_research.stock_daily_highlight.badges_json is
  'view-ready badge chips [{key, label_zh, tone}]';
