# Research note: TypeSafe and Jev

Read 2026-09-25 for Step 5 planning. Sources are the live TypeSafe docs (the
source of truth per `CLAUDE.md`), plus independent write-ups. Anything marked
**vendor claim** comes from TypeSafe or a page that repeats TypeSafe's numbers.
Anything marked **verify** must be checked against the live docs when the code
is written; this note is a map, not the contract.

Docs index: https://docs.typesafe.ai/llms.txt. Pages can be fetched as
Markdown by appending `.md`.

## 1. What Jev is

- Jev is TypeSafe's first **System One** model: it does not write text. You
  send a `state` (string, object or array of text) and named typed
  `questions`; you get back typed answers with probabilities. Early access
  opened 2026-09-15.
- Three question types:
  - **Noul**: yes/no. Returns one number, P(yes). No separate confidence.
  - **Choice**: one of up to **255** options. Returns the choice, a
    probability per option, and `confidence`.
  - **Score**: position on up to **10** ordered levels. Returns the score, a
    probability per level, and `confidence`.
- All questions in one request see the same state, run **in parallel**, and
  cannot see each other. Batching gives the same answers as separate calls
  (their own test: 13 questions, 12x cheaper, 10x faster) because the state is
  sent once.
- `confidence` is computed from how concentrated the probability is. It is a
  statistic about the distribution, **not** proof the answer is right and
  **not** permission to act.
- Trained (RLCD) so probabilities are calibrated across many predictions. That
  is not a guarantee about any single answer.
- **Code owns the workflow.** TypeSafe's own design guide says System One is
  for "AI-powered software, not agents": deterministic control flow in code,
  the model only for narrow judgments. That matches our priority 3
  (deterministic, auditable decisions).

## 2. Numbers that matter to us

| Item | Value | Source |
| --- | --- | --- |
| Current model | `jev-1.13.0` (aliases `jev-latest`, `jev-preview` move) | Models page |
| Price | **$0.042 per million input tokens; output tokens free** | Models page |
| Latency | most calls about 100 ms; independent reports 70-500 ms | docs; third parties |
| Rate limits | 250,000 tokens/s and 1,200 requests/min; **"adjusting dynamically"** | Models page (verify) |
| Context | 64k tokens per request; 32k for state plus the longest question | Models page |
| Input | text only; English is best, other languages lower accuracy | Models page |
| Also offered via | OpenRouter (`~typesafe/jev-latest`, listed at the same price) and Vercel AI Gateway | SDK usage page; search results |

Rough cost, our workload: a judgment with a 2,000-token state costs about
$0.000084. 10,000 judgments a day is about $0.84/day, about $25/month.
Batching several questions over one state costs the state once. This is small
next to the model calls the judgments protect, which is the point: Jev is the
cheap verifier that lets the cheap model be trusted more often.

## 3. Known weaknesses (Jev 1.13 "jaggedness", reviewed 2026-09-17)

Each one becomes a design rule for us.

| Weakness | Our rule |
| --- | --- |
| Reads questions **literally** | Write the exact condition; put boundary cases in `criteria`. |
| Bad at **counting and arithmetic** | Do all numbers in code. Ask one Noul per item and add up in code. |
| Bad at **date/time comparison** | Extract components, compare in code. |
| Weak on **indirection** (double negatives, multi-hop) | One direct, atomic question; name the state field it refers to. |
| Accuracy falls with **irrelevant state** | Filter and retrieve in code first; send only what the question needs. |
| **Adversarial content can steer it** ("text that argues for its own classification") | Never let Jev be the only defence against prompt injection. It is one layer; see the design in Step 5.3. |
| **Contradictory** instructions/criteria confuse it | Instructions and criteria say the same thing. |
| Answers **not structurally consistent** across question types (a Noul and a Choice on the same idea can disagree) | Ask each decision one way. Do not carry a threshold from a Noul to a Choice. Do not expect `P(x) + P(not x) = 1`. |
| **Cannot generate** text, and gives **no explanation** | Use it to select and verify, never to write. Log inputs and answers so a human can inspect a decision. |

## 4. Data handling: this is a constraint on our design

- TypeSafe commits **not to train or fine-tune on your inputs** (Privacy
  Policy; DPA).
- Neither document states a **default retention period or deletion timeline**
  for request content. The DPA says only "as long as necessary". A
  **zero-data-retention (ZDR) option exists for enterprise customers only**.
  Subprocessor list: https://trust.typesafe.ai/subprocessors (15 days' notice
  of changes; breach notice within 72 hours).
- `CLAUDE.md`: sensitive data goes only to no-retention, no-training
  providers. Until TypeSafe confirms retention in writing
  (privacy@typesafe.ai), **treat TypeSafe as not approved for sensitive
  data**. Step 5.1 makes this a per-gate switch that defaults to off.
- Jev is not fine-tuned per customer. You shape it only through the state and
  the wording of instructions and criteria, so those are our tuning surface and
  belong in versioned data, like prompts (ADR 007).

## 5. Pin the model

Aliases move when a release ships, and answers change with them. The
response's `model` field reports the exact version. We pin `jev-1.13.0` in
config, log the version on every judgment, and move only after re-running the
calibration set (Step 5.4). This is the same rule ADR 003 applied to
OpenRouter slugs.

## 6. Evidence, and how much to trust it

- **Independent** (Arize, as summarised):
  - Spam classification: 98.3% accuracy with no training, against 98.4% for a
    TF-IDF model trained on 14,800 labelled emails; the two disagreed on 466
    emails and split them evenly.
  - Calibration: emails scored below 0.1 were 0.1% spam; above 0.9 were 99.9%.
  - Moderation ("NearHere"): 96% against 86% for Gemini Flash-Lite, at
    85 tokens against 910, about 58x cheaper per decision.
  - Weakness they call out: no explanations, so little signal for improving a
    system.
- **Vendor claims:** "200x faster, 400x cheaper" than an LLM judge. Treat as an
  upper bound; our own eval (Step 5.4) is the number that counts.
- **Cookbook results** (TypeSafe's own, on their data): citation checking
  caught all four planted failures; a classification confidence cutoff of 0.9
  split filings into a 90%-accurate half and a 40%-accurate half; a skill
  picker cut wrong-skill loads from 16.8% to 7.3%; a cheap-extract, Jev-verify,
  escalate cascade approached a reasoning model's quality at a fraction of the
  cost. Real evidence of the pattern, not a promise for our data.
- **LangChain** publishes an experimental `langchain_typesafe` package with a
  model-router middleware, a tool-risk-gating middleware and a classifier
  (as summarised). We do **not** adopt it (new dependency, marked
  experimental) but the patterns are the ones we want.

## 7. Cookbook to Pantheon map

| Pantheon need | Pattern and cookbook | Where in the plan |
| --- | --- | --- |
| Is a new fact true to its source? | Citation check: exact string match in code first, then one Choice `supports / contradicts / says nothing`, human review below a confidence floor | 5.2 |
| Is it a duplicate, or does it contradict a stored fact? | Entity alignment: one Score with three outcomes (different / related, possibly same / same), companion Nouls for what disagrees | 5.2 |
| Fact expires or is opinion, hedge, secret, or an instruction to an AI? | Noul battery over one claim (speculative fan-out) | 5.2 |
| Is scraped or uploaded text safe to show an agent? | Classifying RAG passages: per-passage Nouls for relevance, evidence, contradiction, **prompt injection**; injection checked first | 5.3 |
| Screen agent input and output | LLM guardrails: hazard Nouls plus a severity Score, named policies (`strict`, `normal`) as thresholds | 5.3 |
| Which department or agent should take this request? | Intent routing and confidence-gated routing; hierarchical classification with fallback to the broader label | 8.2 |
| Is this tool call safe to run? | Tool-risk gating; confidence thresholds scale with risk | 7.5 |
| Which tool or worker fits this subtask? | Skill suggestion (rank, then re-check the top few); function calling | 7.2, 9 |
| Can the cheap model's answer be trusted? | SDE cascade: cheap model, Jev verifies per field (narrow, "bad = true"), escalate on any flag | 9 |
| Which recalled facts reach the model? | Re-ranking and passage classification | 9 |
| Uncertain results go to a person | Self-consistency cookbooks: an explicit "uncertain" outcome between two thresholds | all gates |

## 8. Integration facts (verify against the live API page when coding)

- `POST https://api.typesafe.ai/v1/systemone`, `Authorization: Bearer <key>`.
  Body: `state`, `model`, `questions` (a map you name). Response: `model`,
  `answers` (same keys), `usage {input_tokens, output_tokens}`.
- Each answer carries `type`; Choice/Score add `probabilities` and
  `confidence`; Noul is `noul` in 0..1.
- `instructions` and `criteria` may be strings or structured objects; refer to
  state fields with backticked paths such as `` `claim.text` ``.
- 429 on rate limit; the docs say to honour `retry-after`.
- Python SDK `typesafe-sdk` exists (sync and async, retry policy, typed
  responses). Env vars: `TYPESAFE_API_KEY`, `TYPESAFE_BASE_URL`,
  `TYPESAFE_DEFAULT_MODEL`.
- **Our recommendation** (open decision 10 in the plan): a thin `httpx`
  transport inside our own package, in the same style as
  `gateway/transport.py`, so there is no new dependency and kill switch, cost
  logging and events stay in one place.

## 9. Open questions to close during Step 5

1. Retention of request content, in writing (email above).
2. Whether rate limits stabilise, or we need a paid plan; whether a production
   SLA exists.
3. Whether the model returns any request id we can store for support.
4. The owner's `TYPESAFE_API_KEY` (currently empty in `.env`).

## Sources

- [TypeSafe docs index](https://docs.typesafe.ai/llms.txt), Models, Confidence,
  How to build with TypeSafe, Jev 1.13 jaggedness, API reference, Python SDK,
  Patterns, and the Cookbooks named above
- [Introducing System One Models & Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev)
- [TypeSafe Data Processing Agreement](https://typesafe.ai/legal/data-processing)
  and [Privacy Policy](https://typesafe.ai/legal/privacy-policy)
- [Arize: Can Decision Models Replace LLM Judges?](https://arize.com/blog/typesafe-jev-llm-judge/)
- [LangChain: What Is Jev? Building a harness with Jev](https://www.langchain.com/blog/building-a-harness-with-jev)
- [MarkTechPost launch coverage](https://www.marktechpost.com/2026/09/19/typesafe-ai-releases-jev/)
- [OpenRouter tool calling](https://openrouter.ai/docs/guides/features/tool-calling)
  (for the agent-platform steps)
