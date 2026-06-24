-- yahoo-daily-cron · pg_cron · once after TW close (16:30 Asia/Taipei = 08:30 UTC, Mon–Fri)

select cron.unschedule(jobid)
from cron.job
where jobname = 'yahoo-daily-cron-post-close';

select cron.schedule(
  'yahoo-daily-cron-post-close',
  '30 8 * * 1-5',
  $$
  select net.http_post(
    url := (select decrypted_secret from vault.decrypted_secrets where name = 'project_url')
           || '/functions/v1/yahoo-daily-cron',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name = 'publishable_key')
    ),
    body := '{}'::jsonb
  ) as request_id;
  $$
);
