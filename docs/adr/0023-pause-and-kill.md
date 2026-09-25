# ADR 023: A pause switch and a kill

- **Status:** Accepted (owner, 2026-09-26)
- **Date:** 2026-09-26
- **Step:** 7.6 follow-up

## Context

The `kill_switch` flag (ADR 002, 006, 022) stops every model call, run start,
new task and wake-up, and runs it stopped can be resumed afterwards. The
owner pointed out that this is a pause, not a kill: a real kill switch must
end everything, with nothing left to come back.

## Decision

Two controls, both only for a person in the org (never an agent's session):

- **Pause** (`set_pause`, `POST /pause`, `scripts.approvals pause on|off`):
  the existing flag. Work stops where it is; after lifting it, the owner
  resumes paused runs (`resume_paused_runs`). The flag keeps its database
  name, `kill_switch`, so no existing check changes; the owner-facing name
  is "pause".
- **Kill** (`kill_everything`, `POST /kill` with `"confirm": "KILL"`,
  `scripts.approvals kill`), in one transaction:
  1. turns the pause on, so nothing new starts and a function still working
     is refused at its next model call;
  2. cancels every unfinished task, then every unfinished run
     (`stop_reason = 'killed'`), so nothing can wake or be resumed;
  3. expires every pending approval and rejects every held or approved but
     unrun tool call, so no held action can be approved into running later;
  4. writes one `killed` event with the counts.

A function invocation that was mid-step when the kill landed stops at its
next step boundary (it now checks whether its run was cancelled), and its
final report can no longer overwrite `cancelled` with another status.

Triggers, agents and settings are untouched. After a kill the owner lifts
the pause when ready; only new work (the next scheduled trigger or order)
runs.

## Consequences

- A model call already in flight when the kill lands still completes and is
  paid for; nothing after it runs. A tool call already executing finishes;
  irreversible tools are approval-held, so none can be mid-execution without
  the owner having approved it.
- `is_backend()` replaces a `current_user` check that never matched inside
  SECURITY DEFINER functions (it refused the backend's own calls to
  `resume_paused_runs`).
