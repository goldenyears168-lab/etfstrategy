-- yahoo-cron · pg_cron · TW market session (Asia/Taipei 09:00–13:30, Mon–Fri)
-- Edge Function also skips outside session; this reduces idle invocations.
--
-- Prereq: vault secrets project_url + publishable_key (see Supabase schedule-functions doc).
-- Legacy job names finmind-cron-* removed in 015_yahoo_cron_rename.sql if present.

select cron.unschedule(jobid)
from cron.job
where jobname in ('yahoo-cron-session-early', 'yahoo-cron-session-late');

-- 09:00–12:59 TW = 01:00–04:59 UTC
select cron.schedule(
  'yahoo-cron-session-early',
  '*/15 1-4 * * 1-5',
  $$
  select net.http_post(
    url := (select decrypted_secret from vault.decrypted_secrets where name = 'project_url')
           || '/functions/v1/yahoo-cron',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name = 'publishable_key')
    ),
    body := '{}'::jsonb
  ) as request_id;
  $$
);

-- 13:00–13:30 TW = 05:00–05:30 UTC
select cron.schedule(
  'yahoo-cron-session-late',
  '0,15,30 5 * * 1-5',
  $$
  select net.http_post(
    url := (select decrypted_secret from vault.decrypted_secrets where name = 'project_url')
           || '/functions/v1/yahoo-cron',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name = 'publishable_key')
    ),
    body := '{}'::jsonb
  ) as request_id;
  $$
);

comment on table public.yahoo_quotes is
  'Yahoo Chart API cache · ~135 ETF universe · 15m TTL · yahoo-cron session-only';
