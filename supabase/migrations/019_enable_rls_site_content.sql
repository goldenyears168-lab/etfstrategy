-- Enable RLS on tables that already have public-read policies (Supabase advisor 0007).

alter table stock_research.site_content enable row level security;
alter table stock_research.strategy_performance_yearly enable row level security;
