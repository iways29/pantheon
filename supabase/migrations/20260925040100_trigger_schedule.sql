-- Wire the scheduler: enable pg_cron and pg_net and start the one-minute tick.
--
-- ADR 008. Split from the tables and functions so those stay testable on a
-- plain PostgreSQL server, which has neither extension. Where they are not
-- available (local development, CI) this migration does nothing and says so.
--
-- Safe to apply before anything is configured: the tick only creates runs for
-- triggers the owner has enabled (none exist yet), and does not call the API
-- until the owner has stored its address and secret in Vault.

do $$
begin
  if exists (select 1 from pg_available_extensions where name = 'pg_cron')
     and exists (select 1 from pg_available_extensions where name = 'pg_net')
  then
    create extension if not exists pg_cron with schema pg_catalog;
    create extension if not exists pg_net with schema extensions;

    -- Upserts by name, so re-applying never creates a second tick.
    perform cron.schedule('pantheon-tick', '* * * * *', 'select public.pantheon_tick()');

    -- pg_cron never trims its own run history, and a job that runs every
    -- minute writes 1,440 rows a day.
    perform cron.schedule(
      'pantheon-cron-cleanup',
      '17 4 * * *',
      $cleanup$delete from cron.job_run_details where end_time < now() - interval '7 days'$cleanup$
    );
  else
    raise notice 'pg_cron / pg_net are not available here; the scheduler is not wired.';
  end if;
end;
$$;
