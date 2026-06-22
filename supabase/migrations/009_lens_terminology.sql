-- Terminology: delta_new_to_lens → delta_new_to_watchlist（監控清單）

do $$
begin
  if exists (
    select 1
    from information_schema.columns
    where table_schema = 'stock_research'
      and table_name = 'stock_daily_lens'
      and column_name = 'delta_new_to_lens'
  ) then
    alter table stock_research.stock_daily_lens
      rename column delta_new_to_lens to delta_new_to_watchlist;
  end if;
end $$;
