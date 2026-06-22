-- Per-stock signal index for Readdy stock search (replaces content_md ilike)
-- Builder: src/stock_signal_index.py · sync: src/supabase_signal_sync.py

create table if not exists stock_research.stock_signal_hits (
  trade_date date not null,
  stock_id text not null,
  brief_type text not null,
  schedule_slot text not null default '1630',
  source text not null,
  stock_name text,
  tab text,
  layer_label text,
  brief_label text,
  row_json jsonb not null default '{}'::jsonb,
  headline_zh text,
  computed_at timestamptz not null default now(),
  primary key (trade_date, stock_id, brief_type, schedule_slot)
);

create index if not exists stock_signal_hits_stock_date_idx
  on stock_research.stock_signal_hits (stock_id, trade_date desc);

create index if not exists stock_signal_hits_name_idx
  on stock_research.stock_signal_hits (stock_name);

alter table stock_research.stock_signal_hits enable row level security;

grant select on stock_research.stock_signal_hits to anon, authenticated;
grant all on stock_research.stock_signal_hits to service_role;

create policy "stock_signal_hits_public_read"
  on stock_research.stock_signal_hits
  for select to anon, authenticated using (true);

comment on table stock_research.stock_signal_hits is
  'Stock × trade_date × brief_type index for search · built from snapshot_json + lens';
