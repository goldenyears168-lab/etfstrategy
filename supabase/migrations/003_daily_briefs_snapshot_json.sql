-- Regime daily · structured snapshot for React / mobile (Recharts / D3)
-- schema stock_research · complements content_html

alter table stock_research.daily_briefs
  add column if not exists snapshot_json jsonb;

create index if not exists daily_briefs_snapshot_json_gin
  on stock_research.daily_briefs
  using gin (snapshot_json);

comment on column stock_research.daily_briefs.snapshot_json is
  'Regime four-axis diagnostic · regime-snapshot-v1 (axes + chart_series + interpretations)';
