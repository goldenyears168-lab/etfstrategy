-- 前端（Readdy · anon key）唯讀 daily_briefs
-- Project: lzaomqzsiqudkojokevr · schema stock_research
-- service_role sync 不受影響（已有 ALL grant）

grant usage on schema stock_research to anon, authenticated;
grant select on stock_research.daily_briefs to anon, authenticated;

create policy "daily_briefs_public_read"
  on stock_research.daily_briefs
  for select
  to anon, authenticated
  using (true);
