-- Let the backend's own login reach checkpoints without SET ROLE.
--
-- ADR 005, after probing Supabase's transaction pooler: it honours a
-- connect-time search_path but drops a connect-time role, so the checkpointer
-- runs as the backend's login role rather than as service_role. On Supabase
-- that login is `postgres`, which owns these tables and bypasses RLS, so this
-- policy changes nothing there. It is for any backend login that is merely a
-- member of service_role (the local test role, for one): membership grants
-- the table privileges, and this grants the rows.
--
-- anon and authenticated remain without any privilege on the schema.

create policy checkpoints_backend on langgraph.checkpoints
  for all to service_role using (true) with check (true);
create policy checkpoint_blobs_backend on langgraph.checkpoint_blobs
  for all to service_role using (true) with check (true);
create policy checkpoint_writes_backend on langgraph.checkpoint_writes
  for all to service_role using (true) with check (true);

-- The org_id trigger looks the run up in public.runs, which has RLS. It used
-- to run as service_role and bypass that; as the plain login role it would
-- see no run and refuse every checkpoint. SECURITY DEFINER makes the lookup
-- run as the function's owner, whoever writes the checkpoint. It reads one
-- column of one row by primary key, and its search_path is already pinned.
alter function langgraph.set_org_id_from_run() security definer;
