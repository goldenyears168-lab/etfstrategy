-- Phase 2 · 移除 0 筆幽靈表（前端只讀 stock_research schema）
-- 不影響 public.booking_logs 等官網預約表

drop table if exists public.daily_briefs;
drop table if exists public.site_content;

-- repo 無引用 · 0 筆 staging
drop table if exists stock_research._snapshot_upload_staging;
