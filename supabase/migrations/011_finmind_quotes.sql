-- Yahoo quote cache · public schema · Readdy edge functions + useYahooQuote
-- Writer: yahoo-quote / yahoo-cron (service_role) · Reader: anon SELECT

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

drop policy if exists "yahoo_quotes_public_read" on public.yahoo_quotes;

create policy "yahoo_quotes_public_read"
  on public.yahoo_quotes
  for select to anon, authenticated using (true);

comment on table public.yahoo_quotes is
  'Yahoo Chart API quote cache · 15m TTL · yahoo-cron / yahoo-quote upsert';
