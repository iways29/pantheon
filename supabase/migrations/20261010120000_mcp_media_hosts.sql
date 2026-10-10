-- What an MCP tool made reaches the owner even when its text is withheld
-- (ADR 040).
--
-- An image service answers "job started, now call jobs_wait" — words aimed at
-- the AI caller, which the injection screen rightly quarantines, and the job
-- id went with them. Now the screen's verdict covers the text only: ids
-- (UUIDs) are always kept, and https links are kept when their host is one
-- the owner trusts for that server. Nothing else of a withheld reply is.

alter table public.mcp_servers
  add column media_hosts text[] not null default '{}'
    check (cardinality(media_hosts) <= 10);

comment on column public.mcp_servers.media_hosts is
  'Hosts whose https links in this server''s replies are kept (and shown to the '
  'owner as media) even when the reply''s text is withheld. Set by the owner.';

create or replace function public.audit_mcp_server()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  subject public.mcp_servers;
begin
  if tg_op = 'DELETE' then
    if not exists (select 1 from public.orgs where id = old.org_id) then
      return old;
    end if;
    subject := old;
  else
    subject := new;
  end if;
  if tg_op = 'UPDATE' and (new.url, new.auth, new.status, new.enabled, new.media_hosts)
                          is not distinct from
                          (old.url, old.auth, old.status, old.enabled, old.media_hosts) then
    return new;
  end if;
  insert into public.events (org_id, type, payload)
  values (subject.org_id, 'mcp_server_' || lower(tg_op),
          jsonb_build_object('server', subject.name, 'url', subject.url, 'auth', subject.auth,
                             'status', subject.status, 'enabled', subject.enabled,
                             'media_hosts', to_jsonb(subject.media_hosts),
                             'changed_by', auth.uid(), 'db_role', current_user));
  return subject;
end;
$$;
