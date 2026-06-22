-- Cross-layer stock_daily_lens + lens_daily_alert · Readdy / email digest
-- SSOT builder: src/stock_daily_lens.py · sync: src/supabase_lens_sync.py

create table if not exists stock_research.stock_daily_lens (
  trade_date date not null,
  stock_id text not null,
  stock_name text,

  etf_add_count int not null default 0,
  etf_reduce_count int not null default 0,
  etf_add_codes text[] not null default '{}',
  etf_flow_ntd numeric,
  share_delta_total numeric,
  growth_pct numeric,
  consensus_add boolean not null default false,
  consensus_streak_days int not null default 0,

  breadth_zone_200 text,
  trend_posture text,
  regime_aligned boolean not null default false,

  rrg_quadrant text,
  rrg_quadrant_prev text,
  rrg_mono_fresh boolean not null default false,
  rrg_tier2 boolean not null default false,
  vcp_composite numeric,
  vcp_execution_state text,
  vcp_distance_pivot_pct numeric,
  copytrade_l1h9_signal boolean not null default false,

  delta_new_to_watchlist boolean not null default false,
  delta_rrg_quadrant_change text,
  delta_consensus_new_today boolean not null default false,
  delta_score_change numeric,
  delta_any_signal boolean not null default false,

  signal_convergence int not null default 0,
  lens_score numeric not null default 0,
  narrative_zh text not null default '',
  highlight_tier text not null default 'none',

  holdings_aligned boolean not null default true,
  data_baseline_date date not null,
  sources_json jsonb not null default '{}'::jsonb,
  computed_at timestamptz not null default now(),

  primary key (trade_date, stock_id)
);

create index if not exists stock_daily_lens_date_delta_idx
  on stock_research.stock_daily_lens (trade_date, delta_any_signal desc, signal_convergence desc);

create index if not exists stock_daily_lens_date_convergence_idx
  on stock_research.stock_daily_lens (trade_date, signal_convergence desc)
  where highlight_tier = 'fire';

create table if not exists stock_research.lens_daily_alert (
  trade_date date primary key,
  fire_count int not null default 0,
  delta_new_count int not null default 0,
  headline_zh text not null,
  items_json jsonb not null default '[]'::jsonb,
  computed_at timestamptz not null default now()
);

alter table stock_research.stock_daily_lens enable row level security;
alter table stock_research.lens_daily_alert enable row level security;

grant select on stock_research.stock_daily_lens to anon, authenticated;
grant select on stock_research.lens_daily_alert to anon, authenticated;
grant all on stock_research.stock_daily_lens to service_role;
grant all on stock_research.lens_daily_alert to service_role;

create policy "stock_daily_lens_public_read"
  on stock_research.stock_daily_lens
  for select to anon, authenticated using (true);

create policy "lens_daily_alert_public_read"
  on stock_research.lens_daily_alert
  for select to anon, authenticated using (true);

comment on table stock_research.stock_daily_lens is
  'Cross-layer surveillance · delta · signal_convergence · narrative_zh';
comment on table stock_research.lens_daily_alert is
  'Daily lens headline for banner / email digest';
