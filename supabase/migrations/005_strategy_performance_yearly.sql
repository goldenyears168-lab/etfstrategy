-- 已採納策略 · 分年績效（2025/2026）· 組合層級回測指標
-- SSOT：SQLite strategy_performance_yearly → 同步至此表

create table if not exists stock_research.strategy_performance_yearly (
  strategy_id text not null,
  year_label text not null,
  window_start date not null,
  window_end date not null,
  capital_ntd numeric not null,
  n_slots int,
  hold_days int,
  total_return_pct numeric not null,
  cagr_pct numeric,
  win_rate_vs_bench_pct numeric,
  sharpe_ratio numeric,
  mean_excess_pct numeric,
  n_periods int not null,
  benchmark text not null default 'IX0001',
  partial_year boolean not null default false,
  metrics_json jsonb,
  computed_at timestamptz not null default now(),
  primary key (strategy_id, year_label)
);

create index if not exists strategy_performance_yearly_strategy_idx
  on stock_research.strategy_performance_yearly (strategy_id, year_label);

grant select on stock_research.strategy_performance_yearly to anon, authenticated;
grant all on stock_research.strategy_performance_yearly to service_role;

create policy "strategy_performance_yearly_public_read"
  on stock_research.strategy_performance_yearly
  for select
  to anon, authenticated
  using (true);

comment on table stock_research.strategy_performance_yearly is
  '已採納策略分年績效 · 年化報酬率 · 勝率 · Sharpe；靜態頁見 site_content';
