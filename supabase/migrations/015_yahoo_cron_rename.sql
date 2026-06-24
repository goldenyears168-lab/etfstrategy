-- finmind-cron → yahoo-cron · for projects that already applied 014 with legacy job names

select cron.unschedule(jobid)
from cron.job
where jobname in ('finmind-cron-session-early', 'finmind-cron-session-late');

select cron.unschedule(jobid)
from cron.job
where jobname in ('yahoo-cron-session-early', 'yahoo-cron-session-late');

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
