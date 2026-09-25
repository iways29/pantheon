# Company brief: The Unreal Lab

Learned 2026-09-25 from the owner's own materials (the live site, Mumba.ai
rendered in a browser, the site's design and build handoffs, the local
`unreal-lab-os` graph design, the project's saved notes) and the owner's answers
to the questions in section 7. Quotes are the
owner's own public copy. **Open questions are at the end.** Nothing here is
confirmed by the owner yet beyond what their own documents say; treat it as a
draft for them to correct.

This brief is also the seed for the first **company documents** in the brain
(Step 6): positioning, voice, banned claims and the verse library.

## 1. What the business is

A **venture studio**, repositioned from an "AI product lab", with a longer-term
aim of becoming a **venture fund with limited partners (LPs)**. The site says:
"Not a fund yet. A charioteer first."

Four practices, in the site's words:

| Practice | What it means |
| --- | --- |
| **We build** | Its own AI products, shipped and live: **Mumba.ai** (section 1.1) and **ASHVAA** (open-source codebase intelligence, MIT licensed; early stage). "The next one is unnamed." |
| **We advise** | Enterprise AI "that is not allowed to misbehave": governance, evaluation, agent platforms. |
| **We partner** | Building relationships with product companies, aiming to deploy the right ones inside the enterprises its partners serve. Deliberately **forward-looking**. |
| **We back** | Founders "at the very beginning, before there is a deck or a company": architecture, people, introductions, launch help and hours, for "a small agreed stake"; capital "as the portfolio earns it". |

### 1.1 The products

**MUMBAAI** (mumba.ai), "The Branching AI Canvas". "One Question. Every Direction."
AI chat as a canvas of parallel thoughts: branch any reply, run several threads
side by side each on a model of your choice (Claude, GPT or Gemini), see every
branch on one map, and **Smart Merge** the best branches into one answer. Free
during beta, no credit card. Footer: "A product of The Unreal Lab, an AI venture
studio." It is **building in public**: a public five-phase roadmap (proof of
concept and MVP shipped; now real users, personal storage and Smart Merge; next
growth with subscriptions and custom models in Q3 2026; later deep research,
artifacts and MCP connectors; then enterprise) and an on-page feedback form.
Its voice is a crisp product voice in short dramatic fragments ("Chat Is A
Straight Line. Your thinking isn't."), different from the studio's mythic voice.

**ASHVAA** (github.com/iways29/ASHVAA), open-source codebase intelligence: reads a
GitHub repository and gives a dependency map, vulnerability and dead-code
findings, and an AI health score. MIT licensed; early stage; plain technical
README.

Path: **Now** a studio that builds and backs a handful of founders with hours,
**Next** a portfolio that speaks for itself, **Then** a fund with LPs. The site
says it is "looking for our first LPs now".

## 2. Who it talks to

1. **Young founders**, pre-company or too early for everyone else. Door: apply
   by email; "No deck required"; "when the model fits we answer within a week".
2. **Backers**: high-net-worth individuals and future LPs. Door: start a
   conversation ("Be early with us").
3. **Enterprise buyers and product companies** (advisory and partnership).

The owner's stated audience note: "young founders are coming here, also HNIs;
make something that inspires both". Every piece of copy should address both.

## 3. Positioning, voice and language

- **Idea:** the studio is the charioteer, the founder is Arjuna: **"Every Arjuna
  needs a Krishna."** Second line: **"We make the unreal real."**
- **Second story:** Chhatrapati Shivaji Maharaj and *swarajya* built one fort at
  a time: "Swarajya was not declared. It was built, one fort at a time."
- **Voice:** philosophical and practical, poetic but **confident without hype**.
  Restraint is the brand. In the owner's graph seed the studio-voice document
  reads "Precise, unhurried, no hype." Examples from the site:
  "You already hold the bow. You are only doubting your right to draw it." /
  "Access is the advantage no term sheet lists." / "Speed where the incumbents
  were slow."
- **Recurring themes:** the chariot, the field, the bow, forts, swarajya
  (self-reliance), access and depth over fundraising narrative, real execution.
- **Verses** are quoted in Devanagari with a translation and a chapter and verse
  citation (for example Bhagavad Gita 18.78, 2.3, 2.47) **on the Unreal Lab
  website only** (owner, 2026-09-26).
- **Language: everything else is in English** (owner, 2026-09-26). The
  newsletter, blog, X, Reddit and Instagram carry no Sanskrit: the daily
  audience would not follow it. Mahabharata and other mythological themes are
  welcome, told in English; names such as Arjuna and Krishna are fine.
- **Look** (for anything visual): ink `#100c08`, paper `#f4efe4`, body
  `#eadfc4`, gold `#e6c76a`; Libre Caslon Display italic, Archivo, Tiro
  Devanagari Sanskrit; radius 0, no shadows; "bold restraint".

## 4. Hard content rules (from the owner's own notes)

These become the Marketing and Content department's **banned-claims and voice
rules** (design doc 8.1) and Jev guardrail questions (Step 5.3):

1. **"We partner" stays forward-looking.** Never present it as an existing
   deployment pipeline, and never name an employer. (Design handoff, copy note.)
2. **Sanskrit appears only on the website, and only from a vetted verse
   library**, each verse with its chapter and verse and an owner-approved
   translation. Models garble Sanskrit, so an agent never writes, translates or
   "corrects" a verse. In every other channel, content is in English only:
   agent-written text containing Sanskrit or Devanagari is **blocked** by the
   output guardrail (and by a code check for Devanagari), and a quoted verse,
   even in English, is held for the owner. Mythological themes told in English
   are encouraged. (Owner, 2026-09-26.)
3. **No hype.** No superlatives, guarantees or hustle language.
4. **Fund and LP language is regulated.** Text that solicits investment, promises
   returns, or describes fund performance can raise securities rules on
   general solicitation. This is not legal advice; the owner should confirm with
   counsel what may be published. Until then, any post touching the fund, LPs,
   returns or investing is **always held for the owner** and screened by a Jev
   guardrail ("does this text solicit investment or promise returns?").
5. **No generic assets, and no spending generation credits without approval.**
   The owner was upset when credits were spent on a commodity texture: "don't
   generate shit I can get from a stock image website." Image and video
   generation is an R4 (spending) tool: it needs approval for the specific
   deliverable.
6. **Crispness and continuity** in anything visual (holds up at 2x DPR); no
   pointer-driven motion; fancy lives in entrances and scroll.

## 5. The owner's earlier design of this system (`unreal-lab-os`)

Before Pantheon, the owner sketched an "operating system" for the studio as a
Neo4j graph. It holds only sample data, so **nothing needs migrating**. The
ideas are worth carrying into Pantheon's Postgres brain, and they are the
clearest statement of what the studio wants its agents to do:

| Idea in the sketch | What it means for Pantheon |
| --- | --- |
| **Entities**: `Company`, `Person`, `Investor`; edges `FOUNDED`, `INVESTED_IN`, `ADVISES`, `COMPETES_WITH`; a `find_path` query for **warm-intro paths** | A studio that lives on relationships. Pantheon's brain has facts only. An entity and relationship layer is worth adding (open question 4). |
| **Claims** with `tier` (`public`, `internal`, ...), `status` (`proposed`, then `approved`), `expires_at`, `confidence`, and a source per claim | Matches our facts plus the Step 5.2 write gate. Add a **visibility tier** (public or internal) so public content can only use claims cleared for the public. |
| Agents may only create `proposed` claims; a person or gate approves | Same as our write gate, with the owner in the loop for public-tier claims. |
| **`Artifact` (a post, a channel) `RELIED_ON` claims**; a "blast radius" query: if a claim is wrong, which posts used it? | New for Pantheon, and valuable: record which facts each draft relied on (`artifact_claims`), so a wrong fact can be traced to every piece that used it. Belongs in the Marketing and Content design. |
| **Stale sweep** of expired claims, an **approval queue** feed, a `public` surface for `llms.txt` and JSON-LD | Matches Steps 5.2 (review-after dates), 7.5 (approvals) and the brain-hygiene routine. The public machine-readable surface is a later idea. |
| `Department: Intel`, `Agent: Scout`, `Document kind: voice`, `Campaign` | A scouting department (Research and Intelligence), a voice document, and campaigns as a unit of content work. |

The graph database is **not** in Pantheon's locked stack (Supabase Postgres). Do
not add it; port the model to Postgres (recursive queries handle short paths).

## 6. The studio's personality and voice (the owner's brief)

The owner describes the studio's voice as a **mix of the personalities of three
television characters: Harvey Specter, Bobby Axelrod and Thomas Shelby.** These
are references for *character*, given so the agents understand the studio's
temperament. **Never name, quote, paraphrase or reuse dialogue from them**, in
any output, ever. The traits to capture, in the studio's own words:

- **Composed control.** Calm under pressure; authority that never needs to
  raise its voice. The owner's words from the site already fit: "confident
  without hype".
- **Strategic and long-game.** Talks about the play and the position, not the
  noise. Patient ("one fort at a time").
- **Few words, chosen well.** Short declarative sentences. Says the thing, stops.
- **Loyal to its people.** The founders it backs come first; credit goes to them.
- **Dry, understated edge.** An occasional deadpan line, not jokes or swagger.
- **Earned confidence.** The work and the facts do the boasting. Specifics over
  superlatives.
- **Decisive.** Takes a position and states what to do next.

How this sits with the site's restraint: the personality is the *person*; the
writing stays measured. Composure, not bravado. **Do not** write swagger,
trash talk, threats, "crush the competition", hustle-bro language, catchphrases,
role-play or fake dialogue, or anything that sounds like a quote from a show.
Emoji use per channel is for the owner to set. Sanskrit: website only (rule 2).

Writing rules for the agents: short sentences; one idea per paragraph; concrete
numbers and named things; state the recommendation; no filler openers; cut
hype words; end without a flourish.

Because Jev reads questions literally and cannot judge "vibe" reliably, the
voice check is a **scored rubric with concrete levels** plus a few narrow Nouls
(for example "does the text name a fictional character", "does it use hype
words from the banned list"), a code blocklist of names and phrases, and the
owner's approval. The owner's edits to drafts become the labelled examples that
tune it.

## 7. Channels and cadence (owner, 2026-09-25)

**Reddit, Instagram, X, a newsletter, and blog posts on the site every two weeks
or as needed.** The owner owns all the accounts.

- **Phase 1 posting is by hand.** Agents draft; the owner publishes. No agent
  gets an account, a password or an API key. Automation comes later, per
  channel, behind approval, and only after checking each platform's current
  rules (not yet researched): Reddit's self-promotion and disclosure norms vary
  by community and unlabelled promotion is often removed; Instagram is visual
  first; platforms restrict automated posting and may require disclosing
  AI-generated content.
- **One idea, many shapes.** A blog post every two weeks is the anchor; the
  agents derive the X post or thread, the Reddit-native version (written for the
  community, not as an advert), the Instagram carousel text, and the newsletter
  section from it, plus smaller items as needed.
- **Visuals are code-drawn** (SVG and HTML rendered in the site's tokens), not
  paid generation, following the owner's rule on credits.
- **Build in public** is a natural pillar for Mumba: roadmap updates and feedback
  turned into short posts, with the owner's approval.

## 8. What this changes in the plan

- **Research and Intelligence** scouts what a studio needs (founders and early
  companies, investors, enterprise buyers, AI news). An entity and relationship
  layer is **later** (owner, 2026-09-25); until then, facts only.
- **Marketing and Content** (first) gets the voice and hard rules above, the five
  channels, and a Jev claim-and-guardrail set tuned to them.
- **Pantheon is for the owner to run The Unreal Lab** (owner, 2026-09-25). Onboarding
  other companies and giving them agent employees is a possible future and
  **not fixed**, so only the groundwork (`org_id`, RLS, portable agent code) is
  built and no phase-2 features are.
- **Founder Relations** (scoring inbound founder applications) is **deferred**:
  applications arrive by email and there is nothing to process yet. It stays a
  later candidate.
- **LP and partner relations** is later and sensitive (see the fund-language rule).
- Company documents to upload in Step 6: this brief, the voice guide, the verse
  library with approved translations, the banned-claims and banned-words lists,
  the site and Mumba copy.

## 9. Still open

1. **Voice samples.** Two or three pieces the owner considers exactly right, once
   there are any; and the banned words and claims beyond the rules above.
2. **Emoji use** per channel, and the newsletter's cadence. (Sanskrit is
   decided: website only, rule 2.)
3. **Fund and LP content:** whether counsel's guidance is wanted before anything
   about the fund is published (the rule in section 4 holds meanwhile).
4. **Morning time** (America/New_York) and how much the owner wants in the brief.
