-- RRG universe scores · 13:00 intraday + 16:30 close · per-stock rows
-- Schema stock_research · anon read (Readdy)

create table if not exists stock_research.rrg_universe_scores (
  session_date date not null,
  screen_kind text not null check (screen_kind in ('intraday', 'close')),
  data_baseline_date date not null,
  stock_id text not null,
  stock_name text,
  rs_ratio double precision,
  rs_momentum double precision,
  quadrant text,
  quadrants_json jsonb,
  trend text,
  disp double precision,
  seg_last double precision,
  segs_json jsonb,
  tier2 integer not null default 0,
  mono_tier2 integer not null default 0,
  mono_fresh integer not null default 0,
  daily_pct double precision,
  tick_ok integer,
  synced_at timestamptz not null default now(),
  primary key (session_date, screen_kind, stock_id)
);

create index if not exists rrg_universe_scores_session_idx
  on stock_research.rrg_universe_scores (session_date, screen_kind);

create index if not exists rrg_universe_scores_stock_idx
  on stock_research.rrg_universe_scores (stock_id, session_date desc);

alter table stock_research.rrg_universe_scores enable row level security;

grant select on stock_research.rrg_universe_scores to anon, authenticated;
grant all on stock_research.rrg_universe_scores to service_role;

create policy "rrg_universe_scores_public_read"
  on stock_research.rrg_universe_scores
  for select
  to anon, authenticated
  using (true);

comment on table stock_research.rrg_universe_scores is
  'RRG universe per-stock snapshot · screen_kind intraday|close';
