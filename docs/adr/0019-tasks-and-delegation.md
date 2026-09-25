# ADR 019: The chain of command is durable task rows, limited by the database

- **Status:** Accepted (owner decisions 5 and 13, 2026-09-26)
- **Date:** 2026-09-26
- **Step:** 7.3 (tasks and delegation)

## Context

The agent organization (`docs/design/agent-organization.md`) is a Chief of
Staff, department heads and workers. Options for building it (open decision
5): deepagents subagents (one level, in-process, blocking), a LangGraph
supervisor (the whole tree inside one invocation), or durable database tasks.
Our runs are short and resumable on serverless (ADR 006), so the owner chose
database tasks. For limits (open decision 13) the owner chose depth 3, four
children per task and 50 tasks per department per day.

## Decision

- **`tasks`**: a tree (parent, root, depth), assigned agent and department,
  who created it (owner, a trigger, or an agent), instructions and input, a
  status (`queued`, `running`, `blocked`, `awaiting_approval`, `done`,
  `failed`, `cancelled`), a result, an optional cost ceiling, per-run caps,
  and an idempotency key (a repeated order is one task).
- **Runs advance tasks.** The scheduler (ADR 008) now has three steps each
  minute: due triggers create *tasks* from their templates; each queued task
  gets a run (keyed by task and attempt); runs are woken as before. A run's
  end moves its task by trigger: done if it has no open sub-tasks, blocked if
  it has, failed if the run failed.
- **A head stops, it does not wait.** A head's run creates worker tasks with
  the `create_task` tool and ends; its task is blocked. When the last child
  finishes, a trigger queues the head's task again, and its next run reads
  the children's results. Every level fits a short invocation.
- **Limits live in the database** (`delegation_limits`, per org, audited;
  defaults 3, 4, 50) and a `tasks_guard` trigger enforces them on every
  insert, however it arrives: depth, children per task, tasks per
  department per day, a child's budget within what its parent has left.
  Only `chief_of_staff` and `head` agents (new `agents.role_type`) may
  create tasks, and only under the task they are working on.
- **Agents cannot make agents or schedules.** An agent's session
  (`pantheon.agent_id` set) is refused by RLS on inserting or changing
  agents and triggers, and on configuring tools and limits.
- **Cost per level** is the `task_costs` view: each task's own runs, and its
  whole subtree.
- **Owner orders** are `POST /tasks`, `GET /tasks/{id}` (the tree with
  costs) and `POST /tasks/{id}/cancel` (the unfinished subtree).

## Consequences

- A head's resume is a new run over its task's recorded state and its
  children's results, not the same LangGraph thread. Simpler and robust
  across deploys; the head's own reasoning between runs is what it wrote
  down.
- Step 4's scheduled runs are now tasks with runs; their morning-routine
  behaviour and idempotency are unchanged.
- Runners that plan and delegate with real model calls are Step 7.4.
