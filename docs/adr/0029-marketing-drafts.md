# ADR 029: Marketing drafts, the claim check, and the owner's approval card

- **Status:** Accepted (owner's go to build, 2026-09-26; not yet applied live)
- **Date:** 2026-09-26
- **Step:** 8.3 (Marketing and Content); right-hand idea 3 (the claim check)

## Context

Marketing and Content is the first revenue department. It writes; the owner
posts by hand in phase 1. The business brief (`docs/business/the-unreal-lab.md`)
sets hard rules: no hype, no Sanskrit outside the website, never name the
fictional characters the owner used to describe the voice, fund language always
held, and no invented facts. The draft charter could not have run: its workers
used the `pipeline` runner (a question-answering graph with no tools), its
tools `save_draft` and the checks did not exist, and at autonomy L0 every
`create_task` and `save_draft` (R1) would have waited for the owner.

## Decision

**Drafts are rows (`drafts`).** An agent inserts one as `draft`, may move it
to `blocked` or `ready`, and nothing else. Only a person approves, rejects or
marks one as posted (a trigger enforces it). `ready` is allowed only with a
pending `draft_review` approval card, so every ready draft shows on the
decision desk and in the morning brief. The card and the draft move together
(a trigger): approved keeps the owner's edited text in `owner_body`, a
labelled example of their voice; a rejection keeps the owner's note.

**Facts behind a draft (`artifact_claims`).** `relied_on` rows (what the
writer used) and `checked` rows (which fact the claim check matched to a
sentence). `scripts.draft trace <fact>` lists every draft a wrong fact
reached. A draft may rely only on **public** facts (`facts.visibility`, which
starts internal); only a person clears a fact for public use, as an event.

**Checks (`check_draft`, R1), in order:** code checks (Devanagari; a banned
phrase list in the tool's settings: hype words and the character names);
`guard_output`; the **claim check** (`draft_claim`, one Jev Choice per
sentence against nearby public facts: `not_a_claim`, `supported`,
`contradicted`, `unsupported`); then **voice** (`draft_voice`: narrow Nouls
for hype, swagger, fictional characters and filler, plus three rubric Scores
whose weighted mean is the voice score). Anything blocking marks the draft
`blocked` with the reasons and, for a claim, the sentence and any internal
fact that might support it. Flags (fund language, a doubtful claim) go to the
owner on the card, recommended `look_closer`.

**The department.** Content Lead (cheap), topic researcher (cheap), writer
(standard), editor (cheap), all `deep`, autonomy **L1**: internal work runs,
web reads are checked, image generation (R4) is always approved per piece, and
drafts reach the owner only as approval cards. A morning is four rounds within
the four-sub-task limit: ideas, one draft, check-and-fix by the editor (at most
two fixes), report. 06:45 Monday to Friday, before the 07:15 brief. A
`list_drafts` tool (R0) gives the Lead continuity and the owner's notes.

## Consequences

- Estimated $0.06 to $0.10 a day (writer on the standard tier is most of it);
  Jev checks cost well under a cent per draft. Budget stays $0.25 a day.
- Until the owner clears facts as public, any factual sentence blocks. The
  first mornings will show which facts to clear (`scripts.draft public`).
- Two starting eval sets (`evals/cases/draft_claim.json`, `draft_voice.json`)
  measure the gates live; the owner's edits and verdicts replace them.
- Not built yet: the repeat check (too close to something published) and the
  Step 9 cascade; channel publishing APIs (phase 1 posts by hand).
