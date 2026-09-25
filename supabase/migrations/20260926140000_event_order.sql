-- The audit trail in true order.
--
-- `now()` is the start of the transaction, so every row one transaction
-- writes (a held tool call, its approval, the task's new state; a decision
-- and its effects) got the same timestamp, and their order was lost.
-- `clock_timestamp()` is the moment each row is written. Existing rows are
-- unchanged; new rows sort in the order they happened.

alter table public.events alter column created_at set default clock_timestamp();
alter table public.tool_calls alter column created_at set default clock_timestamp();
alter table public.tasks alter column created_at set default clock_timestamp();
alter table public.runs alter column created_at set default clock_timestamp();
alter table public.facts alter column created_at set default clock_timestamp();
