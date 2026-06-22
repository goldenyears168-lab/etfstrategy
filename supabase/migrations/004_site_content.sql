-- 網站層靜態內容 · 六層介紹 · 策略 catalog（Readdy 讀取）
-- 與 daily_briefs（依 trade_date）分離

create table if not exists stock_research.site_content (
  page_id text primary key,
  layer_id text not null,
  title text not null,
  content_md text not null,
  content_html text,
  role text,
  data_sources text,
  web_v1 text,
  tab_label_zh text,
  tab_label_en text,
  sort_order int not null default 0,
  updated_at timestamptz not null default now()
);

create index if not exists site_content_layer_sort_idx
  on stock_research.site_content (layer_id, sort_order);

grant select on stock_research.site_content to anon, authenticated;
grant all on stock_research.site_content to service_role;

create policy "site_content_public_read"
  on stock_research.site_content
  for select
  to anon, authenticated
  using (true);

comment on table stock_research.site_content is
  'Readdy 靜態頁 · 專案六層介紹 · 策略 catalog；daily brief 見 daily_briefs';
