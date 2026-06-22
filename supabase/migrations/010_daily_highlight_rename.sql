-- 今日亮點表命名 · 統一對外術語
-- 前置 migration 008 · 009

do $$
begin
  if exists (
    select 1 from information_schema.tables
    where table_schema = 'stock_research' and table_name = 'stock_daily_lens'
  ) then
    alter table stock_research.stock_daily_lens rename to stock_daily_highlight;
  end if;

  if exists (
    select 1 from information_schema.tables
    where table_schema = 'stock_research' and table_name = 'lens_daily_alert'
  ) then
    alter table stock_research.lens_daily_alert rename to daily_highlight_alert;
  end if;

  if exists (
    select 1 from information_schema.columns
    where table_schema = 'stock_research'
      and table_name = 'stock_daily_highlight'
      and column_name = 'lens_score'
  ) then
    alter table stock_research.stock_daily_highlight
      rename column lens_score to highlight_score;
  end if;

  if exists (
    select 1 from information_schema.columns
    where table_schema = 'stock_research'
      and table_name = 'stock_daily_highlight'
      and column_name = 'delta_new_to_lens'
  ) then
    alter table stock_research.stock_daily_highlight
      rename column delta_new_to_lens to delta_new_to_watchlist;
  end if;
end $$;

comment on table stock_research.stock_daily_highlight is
  '今日亮點 · 跨層監控清單 · delta · signal_convergence · narrative_zh';
comment on table stock_research.daily_highlight_alert is
  '今日亮點 headline · email digest';
