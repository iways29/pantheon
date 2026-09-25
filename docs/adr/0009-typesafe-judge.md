# ADR 009: The TypeSafe judge, called through the gateway, configured as data

- **Status:** Accepted (owner confirmed decisions 10 and 11 on 2026-09-25)
- **Date:** 2026-09-25
- **Step:** 5.1 (judge core)

## Context

Step 5 adds TypeSafe's Jev as the decision layer: narrow typed judgments
(Noul, Choice, Score) with probabilities. Two open decisions gated it:
how to call TypeSafe (10), and whether sensitive data may go to it (11).
The owner confirmed the plan's recommendation for both. Research and numbers
are in `docs/research/typesafe-jev.md`; the API contract was re-read live
(`docs.typesafe.ai/api.md`, `models.md`) before coding.

## Decision

- **Decision 10: our own thin `httpx` transport** (`gateway/systemone.py`),
  not the `typesafe-sdk` package: no new dependency. It lives in the
  **gateway**, because `CLAUDE.md` says every model call goes through
  it. `Gateway.evaluate` applies the same kill switch, department budget and
  agent sub-cap as `complete`, writes one `model_calls` row
  (provider `typesafe`) and a `model_call` event.
- **Retries are few and short:** at most 2 retries on 429, 5xx and 529,
  honouring `retry-after`, capped at 4 s a wait, 10 s timeout per attempt.
  A **circuit breaker** (5 failures in a row, then 30 s of not calling) lives
  on the transport, one per warm function instance. 4xx refusals are not
  retried and do not trip it.
- **Cost is data.** TypeSafe reports tokens, not cost, so a new `model_prices`
  table holds USD per million tokens (seeded: `jev-1.13.0` at $0.042 input,
  output free). A model with no price is **refused**, never logged at zero.
  Price changes are audited to `events`.
- **Decision 11: no sensitive state to TypeSafe.** `judge_gates.allow_sensitive`
  defaults to false; `Judge.run(..., sensitive=True)` on such a gate is refused
  before any call. The owner may flip it per gate once TypeSafe confirms
  retention in writing.
- **Gates and questions are versioned data**, like prompts (ADR 007):
  `judge_questions` (gate, key, version, type, instructions, criteria) and
  `judge_gates` (version, pinned model, policy, fail mode, sensitive switch,
  size limit, enabled). New edit = new version; only `active` may change
  (column grants and a freeze trigger); activations are audited by trigger;
  rollback is activating an old version. A change applies on the next call.
- **The model is pinned per gate.** The database rejects `-latest` and
  `-preview` aliases. Moving versions waits for the calibration set (5.4).
- **Policy is data, applied in code** (`judge/policy.py`, pure functions).
  Outcomes are listed least to most severe; rules put thresholds on one
  question's answer; the decision is the most severe matching outcome, so
  an "uncertain, ask a person" band is two rules. A policy that does not fit
  the gate's live questions cannot be published, and a question the rules
  still use cannot be retired.
- **Raw and decided are kept apart.** Every answer is a `judgments` row with
  its question version, gate version, model, tokens, latency and a shared
  `request_id`. The decision and its reasons go in the `judgment_made` event.
- **Failure modes.** Unreachable, timeout, rate limited, overloaded, circuit
  open, refused request, or an answer that does not fit the questions: the
  gate's fail mode decides (closed = most severe outcome, the default; open =
  least severe). A malformed answer that was billed is still costed.
- **Refusals raise** (`JudgeError`): gate missing, misconfigured or disabled,
  sensitive state, state over the size limit. So do the gateway's own
  refusals (kill switch, budget, no price). A disabled gate never waves
  things through.

## Consequences

- One Jev call of about 300 tokens costs about $0.0000126; judgments add
  almost nothing to a department's spend, but they do count towards it.
- Until the Control Center, gates are managed with `python -m scripts.judge`.
  `python -m scripts.jev_smoke` makes one live call to check the contract.
- Found while building: with no key, the API answers **403**
  (`authentication_error`), not the 401 its reference lists. Handled the same
  way (not retried). To report to TypeSafe, low priority.
- Judgment refusals and failures are audited in the caller's transaction, as
  gateway refusals are: catch the error inside the transaction to keep the
  event.
- Langfuse sees each Jev call as a generation named `call-model` with the
  state and answers as JSON. Never sensitive, by the rule above.
