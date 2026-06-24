-- Today highlight · featured strategy group for Readdy section headers
-- Builder: src/stock_daily_lens.py · sync: src/supabase_lens_sync.py

alter table stock_research.stock_daily_highlight
  add column if not exists strategy_group_rank int;

comment on column stock_research.stock_daily_highlight.strategy_group_rank is
  '日報精選分組 0=RRG mono · 1=VCP · 2=Copytrade · 3=其他';
