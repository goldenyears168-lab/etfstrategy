-- Rename finmind_quotes → yahoo_quotes (existing projects)

do $$
begin
  if to_regclass('public.finmind_quotes') is not null
     and to_regclass('public.yahoo_quotes') is null then
    alter table public.finmind_quotes rename to yahoo_quotes;
    alter index if exists public.finmind_quotes_updated_at_idx
      rename to yahoo_quotes_updated_at_idx;
    drop policy if exists "finmind_quotes_public_read" on public.yahoo_quotes;
  end if;
end $$;

create table if not exists public.yahoo_quotes (
  code text primary key,
  price numeric,
  change_val numeric,
  change_percent numeric,
  data_date date,
  updated_at timestamptz not null default now()
);

create index if not exists yahoo_quotes_updated_at_idx
  on public.yahoo_quotes (updated_at desc);

alter table public.yahoo_quotes enable row level security;

grant select on public.yahoo_quotes to anon, authenticated;
grant all on public.yahoo_quotes to service_role;

drop policy if exists "finmind_quotes_public_read" on public.yahoo_quotes;
drop policy if exists "yahoo_quotes_public_read" on public.yahoo_quotes;

create policy "yahoo_quotes_public_read"
  on public.yahoo_quotes
  for select to anon, authenticated using (true);

comment on table public.yahoo_quotes is
  'Yahoo Chart API quote cache · 15m TTL · yahoo-cron / yahoo-quote upsert';
