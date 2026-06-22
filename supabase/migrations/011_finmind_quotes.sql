-- FinMind quote cache · public schema · Readdy edge functions + useFinmindQuote
-- Writer: finmind-quote / finmind-cron (service_role) · Reader: anon SELECT

create table if not exists public.finmind_quotes (
  code text primary key,
  price numeric,
  change_val numeric,
  change_percent numeric,
  data_date date,
  updated_at timestamptz not null default now()
);

create index if not exists finmind_quotes_updated_at_idx
  on public.finmind_quotes (updated_at desc);

alter table public.finmind_quotes enable row level security;

grant select on public.finmind_quotes to anon, authenticated;
grant all on public.finmind_quotes to service_role;

create policy "finmind_quotes_public_read"
  on public.finmind_quotes
  for select to anon, authenticated using (true);

comment on table public.finmind_quotes is
  'FinMind TaiwanStockPrice cache · 15m TTL · edge function upsert';
