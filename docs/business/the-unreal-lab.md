# Company brief: The Unreal Lab

Learned 2026-09-25 from the owner's own materials: the live site
(theunreallab.com), the site's design handoff and build handoff, the local
`unreal-lab-os` graph design, and the project's saved notes. Quotes are the
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
| **We build** | Its own AI products, shipped and live: **Mumba.ai** (a tree-based conversation interface) and **ASHVAA** (open-source codebase intelligence, MIT licensed; early stage). "The next one is unnamed." |
| **We advise** | Enterprise AI "that is not allowed to misbehave": governance, evaluation, agent platforms. |
| **We partner** | Building relationships with product companies, aiming to deploy the right ones inside the enterprises its partners serve. Deliberately **forward-looking**. |
| **We back** | Founders "at the very beginning, before there is a deck or a company": architecture, people, introductions, launch help and hours, for "a small agreed stake"; capital "as the portfolio earns it". |

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
  citation (for example Bhagavad Gita 18.78, 2.3, 2.47).
- **Look** (for anything visual): ink `#100c08`, paper `#f4efe4`, body
  `#eadfc4`, gold `#e6c76a`; Libre Caslon Display italic, Archivo, Tiro
  Devanagari Sanskrit; radius 0, no shadows; "bold restraint".

## 4. Hard content rules (from the owner's own notes)

These become the Marketing and Content department's **banned-claims and voice
rules** (design doc 8.1) and Jev guardrail questions (Step 5.3):

1. **"We partner" stays forward-looking.** Never present it as an existing
   deployment pipeline, and never name an employer. (Design handoff, copy note.)
2. **Verses come only from a vetted verse library**, each with its chapter and
   verse and an owner-approved translation. Models garble Sanskrit, and Jev
   cannot check it, so an agent may never write or "correct" a verse itself.
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

## 6. What this changes in the plan

- **Research and Intelligence** should scout what a studio needs: founders and
  early companies, investors, enterprise buyers, and AI news, with entities and
  relationships, not only loose facts.
- **Marketing and Content** (chosen first) gets the voice and hard rules above,
  and a Jev claim-and-guardrail set tuned to them. Its content pillars follow
  the four practices plus the Gita and Shivaji framing; channels are unconfirmed
  (the sketch used **X**).
- **Founder Relations (deal flow)** is a strong candidate department: inbound
  applications by email, scored on a rubric with Jev, with reply drafts the
  owner approves, honouring the "answer within a week" promise. Jev routing and
  scoring fit it directly. Proposed as an early addition after Marketing and
  Content, if the owner agrees.
- **LP and partner relations** is later and sensitive (see rule 4).
- Company documents to upload in Step 6: this brief, the voice guide, the verse
  library with approved translations, the banned-claims list, and the site copy.

## 7. Open questions for the owner

1. **How does Pantheon relate to The Unreal Lab?** Is it the studio's internal
   operating system (the replacement for `unreal-lab-os`), a separate product
   (Phase 2 exposes agents to other companies), or both, with the studio as the
   first customer?
2. **Which channels** should Marketing and Content write for first (X, LinkedIn,
   the site, a newsletter)? Who owns the accounts?
3. **Is Founder Relations wanted early?** Are applications arriving by email now?
4. **Should the brain get an entity and relationship layer** (companies, people,
   investors, introductions), in Step 5 or 6, or later?
5. **Fund and LP content:** does the owner have, or want, counsel's guidance
   before anything about the fund is published?
6. **Mumba.ai:** the site returned no content to the reader used here; a short
   description from the owner would help. ASHVAA is clear from its README.
7. **Voice samples:** two or three posts or passages the owner considers
   exactly right, and words or claims never to use.
