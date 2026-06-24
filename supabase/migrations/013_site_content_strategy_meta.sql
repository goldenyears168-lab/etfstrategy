-- 策略頁動態 registry · frontmatter strategy_id / icon / brief_types 等
-- Readdy 讀 layer_id=strategy 且 strategy_id IS NOT NULL → 策略目錄自動渲染

alter table stock_research.site_content
  add column if not exists strategy_id text,
  add column if not exists icon text,
  add column if not exists description_short text,
  add column if not exists research_page_id text,
  add column if not exists brief_types text[];

create index if not exists site_content_strategy_id_idx
  on stock_research.site_content (strategy_id)
  where strategy_id is not null;

comment on column stock_research.site_content.strategy_id is
  '已採納策略 slug · 對應 strategy_performance_yearly.strategy_id · URL /strategies/:strategy_id';
comment on column stock_research.site_content.icon is
  'Remix Icon class · 策略目錄卡片';
comment on column stock_research.site_content.description_short is
  '策略目錄卡片摘要 · 一兩句';
comment on column stock_research.site_content.research_page_id is
  '採納報告 site_content.page_id · /strategies/:id/lineage';
comment on column stock_research.site_content.brief_types is
  '關聯 daily_briefs.brief_type · 日報 tab 連結';
