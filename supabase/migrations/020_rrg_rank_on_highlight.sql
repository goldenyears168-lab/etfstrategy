-- RRG universe rank on stock_daily_highlight · full-pool rs_ratio ranking
-- Builder: src/stock_daily_lens.py · sync: src/supabase_lens_sync.py

alter table stock_research.stock_daily_highlight
  add column if not exists rrg_rs_ratio numeric,
  add column if not exists rrg_rs_momentum numeric,
  add column if not exists rrg_rank int,
  add column if not exists rrg_total int;

comment on column stock_research.stock_daily_highlight.rrg_rs_ratio is
  'JdK RS-Ratio from rrg_universe_scores (close session)';
comment on column stock_research.stock_daily_highlight.rrg_rs_momentum is
  'JdK RS-Momentum from rrg_universe_scores (close session)';
comment on column stock_research.stock_daily_highlight.rrg_rank is
  'Rank within full RRG universe by rs_ratio DESC (1 = highest)';
comment on column stock_research.stock_daily_highlight.rrg_total is
  'Count of RRG universe stocks with rs_ratio on trade_date (same for all rows)';
