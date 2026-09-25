-- Scheduled triggers: fixed tasks that start agents at fixed times.
--
-- ADR 008. The owner wants agents to do a set routine in the morning and
-- otherwise wait to be told, not run on their own all day. So a trigger is a
-- row: which agent, what task, what time of day, which weekdays, in which time
-- zone. It fires at most once per local day. Everything about when and what is
-- data the owner edits; none of it is code or deployment config.
--
-- The database does the scheduling. Once a minute pg_cron calls
-- pantheon_tick(), which (1) creates a run for every trigger that is due and
-- (2) pokes the API to advance runs that need it. The run row is the durable
-- record: if the poke is lost, the next tick sends another.

create table public.triggers (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  agent_id uuid not null,
  name text not null check (btrim(name) <> ''),
  -- What the agent is asked to do, in the shape the agent's run input takes
  -- (the research agent reads {"question": "..."}).
  task jsonb not null check (jsonb_typeof(task) = 'object'),
  time_of_day time not null,
  -- 0 = Sunday ... 6 = Saturday, as in cron. Default: weekdays.
  days_of_week smallint[] not null default '{1,2,3,4,5}'
    check (cardinality(days_of_week) > 0 and days_of_week <@ array[0, 1, 2, 3, 4, 5, 6]::smallint[]),
  -- An IANA name such as 'Asia/Kolkata'. Evaluated when the row is written, so
  -- an unknown zone is refused rather than silently ignored.
  timezone text not null check (timezone(timezone, now()) is not null),
  -- Off until the owner turns it on. A new trigger never starts on its own.
  enabled boolean not null default false,
  -- If a tick is missed (an outage), the task still runs when it comes back,
  -- but only within this many minutes of its time. After that the slot is
  -- skipped, so a long outage does not release a burst of stale work.
  grace_minutes integer not null default 120 check (grace_minutes between 1 and 1440),
  -- Per-run ceilings, as on any run. Scheduled work is unattended, so they
  -- default lower than a manual run's.
  max_steps integer not null default 25 check (max_steps > 0),
  max_tokens integer not null default 50000 check (max_tokens > 0),
  -- Whose identity the run acts under (RLS applies to the agent as to them).
  run_as uuid not null references auth.users (id) default auth.uid(),
  -- The local date this trigger last fired for. What makes it once a day.
  last_slot date,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (org_id, name),
  foreign key (agent_id, org_id)
    references public.agents (id, org_id) on delete cascade
);

comment on table public.triggers is
  'Scheduled tasks: an agent, a task, a time of day, weekdays and a time zone. '
  'Fires at most once per local day, only while enabled. Changes are written '
  'to events.';
comment on column public.triggers.last_slot is
  'Local date of the most recent firing. A trigger fires only for a date after this one.';

create trigger triggers_set_updated_at
  before update on public.triggers
  for each row execute function public.set_updated_at();

-- Every change to a trigger is an event: it changes what runs unattended.
-- Firing itself only moves last_slot, which is not a change to the trigger.
-- A cascade from an agent or org being deleted must not try to point an event
-- at a row that is going away, so those cases are skipped or unlinked.
create or replace function public.audit_trigger_change()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  subject public.triggers;
  event_type text;
begin
  if tg_op = 'DELETE' then
    subject := old;
    event_type := 'trigger_deleted';
    if not exists (select 1 from public.orgs where id = old.org_id) then
      return old;
    end if;
  else
    subject := new;
    if tg_op = 'INSERT' then
      event_type := 'trigger_created';
    else
      if (new.name, new.agent_id, new.task, new.time_of_day, new.days_of_week,
          new.timezone, new.enabled, new.grace_minutes, new.max_steps, new.max_tokens)
         is not distinct from
         (old.name, old.agent_id, old.task, old.time_of_day, old.days_of_week,
          old.timezone, old.enabled, old.grace_minutes, old.max_steps, old.max_tokens)
      then
        return new;
      end if;
      event_type := 'trigger_updated';
    end if;
  end if;

  insert into public.events (org_id, agent_id, type, payload)
  values (
    subject.org_id,
    (select id from public.agents where id = subject.agent_id),
    event_type,
    jsonb_build_object(
      'trigger_id', subject.id,
      'name', subject.name,
      'enabled', subject.enabled,
      'time_of_day', subject.time_of_day,
      'days_of_week', subject.days_of_week,
      'timezone', subject.timezone,
      'changed_by', auth.uid(),
      'db_role', current_user
    )
  );
  return subject;
end;
$$;

create trigger triggers_audit
  after insert or update or delete on public.triggers
  for each row execute function public.audit_trigger_change();

alter table public.triggers enable row level security;

create policy triggers_select on public.triggers
  for select to authenticated using (public.is_org_member(org_id));
create policy triggers_insert on public.triggers
  for insert to authenticated with check (public.is_org_member(org_id));
create policy triggers_update on public.triggers
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));
create policy triggers_delete on public.triggers
  for delete to authenticated using (public.is_org_member(org_id));

grant select, insert, update, delete on public.triggers to authenticated;
grant select, insert, update, delete on public.triggers to service_role;

-- What a scheduled run needs in order to be woken reliably.
alter table public.runs
  add column wake_count integer not null default 0 check (wake_count >= 0),
  add column last_wake_at timestamptz;

comment on column public.runs.wake_count is
  'How many times the scheduler has poked the API about this run. Capped, so '
  'a run that keeps failing to start is not retried forever.';

-- Create a run for every trigger that is due. Returns the new run ids.
--
-- Due means: enabled, its agent and department enabled, the org kill switch
-- off, today is one of its weekdays, its local time has passed but not by more
-- than its grace, and it has not already fired for today's local date. The
-- run's idempotency key is the trigger and the date, so a second call for the
-- same slot (a retried tick, two overlapping ticks) can only find the run
-- that exists.
--
-- Not SECURITY DEFINER: it runs as whoever calls it. pg_cron runs it as the
-- database owner, and tests as service_role.
create or replace function public.dispatch_due_triggers(p_now timestamptz default now())
returns setof uuid
language plpgsql
set search_path = ''
as $$
declare
  t record;
  local_now timestamp;
  local_date date;
  slot_start timestamp;
  new_run uuid;
begin
  for t in
    select tr.*
      from public.triggers tr
      join public.agents a on a.id = tr.agent_id and a.org_id = tr.org_id
      join public.departments d on d.id = a.department_id and d.org_id = a.org_id
     where tr.enabled and a.enabled and d.enabled
     order by tr.created_at
       for update of tr skip locked
  loop
    if public.kill_switch_on(t.org_id) then
      continue;
    end if;

    local_now := p_now at time zone t.timezone;
    local_date := local_now::date;
    slot_start := local_date + t.time_of_day;

    if extract(dow from local_now)::smallint <> all (t.days_of_week)
       or local_now < slot_start
       or local_now >= slot_start + make_interval(mins => t.grace_minutes)
       or (t.last_slot is not null and t.last_slot >= local_date)
    then
      continue;
    end if;

    insert into public.runs
      (org_id, agent_id, trigger, idempotency_key, input, requested_by, max_steps, max_tokens)
    values
      (t.org_id, t.agent_id, 'schedule', 'trigger:' || t.id || ':' || local_date,
       t.task, t.run_as, t.max_steps, t.max_tokens)
    on conflict (org_id, idempotency_key) do nothing
    returning id into new_run;

    update public.triggers set last_slot = local_date where id = t.id;

    if new_run is not null then
      insert into public.events (org_id, run_id, agent_id, type, payload)
      values (t.org_id, new_run, t.agent_id, 'trigger_fired',
              jsonb_build_object('trigger_id', t.id, 'name', t.name, 'slot', local_date));
      return next new_run;
    end if;
  end loop;
end;
$$;

-- The scheduled runs that need the API to advance them, marking each as poked.
--
-- A run needs a poke when it has not started, when an invocation ran out of
-- time and paused it, or when an invocation died and its lease lapsed. Runs
-- stopped for any other reason (budget, kill switch, a missing prompt) are
-- left alone: poking them every minute would only re-pause them and fill the
-- event log. They resume when someone resumes them. Pokes are spaced 45
-- seconds apart and capped at five per run.
create or replace function public.pending_wakeups(p_limit integer default 20)
returns setof uuid
language sql
set search_path = ''
as $$
  update public.runs r
     set wake_count = r.wake_count + 1,
         last_wake_at = now()
   where r.id in (
     select x.id
       from public.runs x
      where x.trigger = 'schedule'
        and x.wake_count < 5
        and (x.last_wake_at is null or x.last_wake_at < now() - interval '45 seconds')
        and (
          x.status = 'pending'
          or (x.status = 'paused' and x.stop_reason = 'deadline')
          or (x.status = 'running' and x.lease_expires_at < now())
        )
      order by x.created_at
      limit p_limit
        for update skip locked
   )
  returning r.id;
$$;

-- Poke the API about each run that needs it. Reads the API's address and the
-- shared secret from Vault, so neither is in the database schema or in code.
-- pg_net makes the request; its timeout is long because the API advances the
-- run inside the request. Without both secrets it does nothing (and says so),
-- so applying this before the owner has set them is harmless.
--
-- SECURITY DEFINER so it can read Vault as its owner; nobody else may call it.
create or replace function public.push_runs()
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  base_url text;
  secret text;
  target uuid;
  pushed integer := 0;
begin
  select decrypted_secret into base_url
    from vault.decrypted_secrets where name = 'pantheon_api_url';
  select decrypted_secret into secret
    from vault.decrypted_secrets where name = 'pantheon_trigger_secret';
  if base_url is null or secret is null then
    raise warning 'pantheon: runs are waiting but the Vault secrets pantheon_api_url and pantheon_trigger_secret are not both set';
    return 0;
  end if;

  for target in select public.pending_wakeups() loop
    perform net.http_post(
      url := rtrim(base_url, '/') || '/internal/runs/' || target || '/advance',
      headers := jsonb_build_object(
        'Content-Type', 'application/json',
        'Authorization', 'Bearer ' || secret
      ),
      body := '{}'::jsonb,
      timeout_milliseconds := 55000
    );
    pushed := pushed + 1;
  end loop;
  return pushed;
end;
$$;

-- What pg_cron calls each minute.
create or replace function public.pantheon_tick()
returns void
language plpgsql
set search_path = ''
as $$
begin
  perform public.dispatch_due_triggers();
  perform public.push_runs();
end;
$$;

-- These are for the scheduler and the backend, never for a signed-in user or
-- anonymous caller over the Data API.
revoke execute on function public.dispatch_due_triggers(timestamptz) from public, anon, authenticated;
revoke execute on function public.pending_wakeups(integer) from public, anon, authenticated;
revoke execute on function public.push_runs() from public, anon, authenticated, service_role;
revoke execute on function public.pantheon_tick() from public, anon, authenticated, service_role;
grant execute on function public.dispatch_due_triggers(timestamptz) to service_role;
grant execute on function public.pending_wakeups(integer) to service_role;
