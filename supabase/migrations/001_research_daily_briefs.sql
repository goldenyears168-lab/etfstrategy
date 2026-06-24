-- 股市研究 briefs · 13:00 / 16:30 排程成果
-- Project: lzaomqzsiqudkojokevr（好時官網預約）
-- 與官網預約 photobooking.booking_logs 分離 → schema stock_research

create schema if not exists stock_research;

create table stock_research.daily_briefs (
  id uuid primary key default gen_random_uuid(),
  trade_date date not null,
  schedule_slot text not null check (schedule_slot in ('1300', '1630')),
  brief_type text not null,
  title text not null,
  content_md text not null,
  content_html text,
  source_path text,
  synced_at timestamptz not null default now(),
  unique (trade_date, brief_type)
);

create index daily_briefs_trade_date_idx
  on stock_research.daily_briefs (trade_date desc);

create index daily_briefs_slot_date_idx
  on stock_research.daily_briefs (schedule_slot, trade_date desc);

alter table stock_research.daily_briefs enable row level security;

grant usage on schema stock_research to service_role;
grant all on stock_research.daily_briefs to service_role;

comment on schema stock_research is
  '股市研究 · 與官網預約 photobooking.booking_logs 分離';
comment on table stock_research.daily_briefs is
  '13:00 VCP+RRG · 16:30 ETF+Regime scheduled briefs';

-- PostgREST exposed schema (Supabase API)
alter role authenticator set pgrst.db_schemas = 'public, graphql_public, stock_research';
notify pgrst, 'reload config';
