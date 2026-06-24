-- yahoo_daily_bars · public schema · daily OHLCV cache (Yahoo Chart API)

create table if not exists public.yahoo_daily_bars (
  code text not null,
  trade_date date not null,
  open numeric,
  high numeric,
  low numeric,
  close numeric not null,
  volume bigint,
  source text not null default 'yahoo',
  updated_at timestamptz not null default now(),
  primary key (code, trade_date)
);

create index if not exists yahoo_daily_bars_code_date_idx
  on public.yahoo_daily_bars (code, trade_date desc);

alter table public.yahoo_daily_bars enable row level security;

grant select on public.yahoo_daily_bars to anon, authenticated;
grant all on public.yahoo_daily_bars to service_role;

drop policy if exists "yahoo_daily_bars_public_read" on public.yahoo_daily_bars;

create policy "yahoo_daily_bars_public_read"
  on public.yahoo_daily_bars
  for select to anon, authenticated using (true);

comment on table public.yahoo_daily_bars is
  'Yahoo Chart API daily OHLCV · ~3mo · yahoo-daily-cron once after close';
