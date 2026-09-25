# Design: Jev as the owner's right hand

Status: **accepted by the owner** (2026-09-26: "put them all in"). Nothing here
is built yet; each idea names the step that builds it. Written in plain terms
on purpose.

## The idea in one paragraph

Jev is fast, cheap (about 2 cents per 1,000 judgments) and gives calibrated
yes/no, pick-one and score answers. It cannot write, explain, do sums or
compare dates, and clever text can sway it. So as the owner's right hand, Jev
is the **instinct**: it recommends, routes, checks and ranks. A cheap language
model writes the explanation the owner reads. Code carries out whatever is
decided, and the owner keeps the final say on anything that cannot be undone.
Over time, the owner's own decisions become the labels that tell us how far
Jev's recommendations can be trusted, type of action by type of action.

## The seven ideas

| # | Idea | What the owner sees | Built in |
|---|---|---|---|
| 1 | **Decision desk** | Every decision that needs the owner arrives as a card: Jev's recommendation (approve, reject, look closer) with its probability, the brain facts it checked, and the most similar past decisions and how the owner decided them. One tap to decide. | Backend 7.5, screen 11 |
| 2 | **"Would you approve this?"** | Per type of action (post to X, reply to a founder, publish a blog), how often Jev's recommendation matched the owner's decision. After enough agreement (starting rule: 30 decisions, 95% agreement) the system *suggests* letting that type run alone. The owner says yes or no; nothing is promoted automatically, and only reversible actions qualify. | 7.5 (tracking), 7.6 (promotion) |
| 3 | **Company fact-checker on everything outgoing** | Every factual claim in a post, email or report is checked against the brain: supported, contradicted, or not in the brain. An unsupported or contradicted claim blocks the draft and says which sentence. Fund language and Sanskrit are handled by the output guardrail (ADR 011). | 8.3 (and every department that writes) |
| 4 | **Contradiction alarm on the owner's own decisions** | The owner's decisions and standing policies are stored as facts (source `owner`). A new order or proposal is checked against them: "you said no paid tools this quarter; this spends credits." Shown on the decision card and in the brief. | 7.5 (decisions become facts), 8.2 (orders checked) |
| 5 | **Front-door routing** | Every order goes to the right department, agent and model tier. A low-confidence routing comes back to the owner as a question rather than a guess. | 8.2 |
| 6 | **Morning priorities** | Overnight items (approvals, findings, drafts, alerts) are scored for urgency, impact and "needs the owner". The brief shows the top five, with the rest one tap away. | 8.2 |
| 7 | **Checking the agents' work** | A cheap model does the work and Jev checks each part with narrow "is this wrong?" questions; only what fails is redone on a stronger model. Cheaper than always using the expensive model, same quality. | 9 |

## Rules that do not bend

- **Irreversible or external actions** (sending, spending, publishing, changing
  code) always wait for the owner (`CLAUDE.md`). Idea 2 can only ever widen
  autonomy for reversible, internal actions, and only on the owner's say-so.
- **A confident answer is not permission.** Only measured agreement with the
  owner (idea 2) earns more freedom, per type of action.
- **Every recommendation is logged**: raw answers in `judgments`, the decision
  and the owner's verdict in `approvals`, both linked. That history is also the
  best source of labels (open decision 14).
- **Nothing marked sensitive goes to TypeSafe** until it confirms retention in
  writing (decision 11). Sensitive decisions still get a card, with no Jev
  recommendation on it.

## How the pieces connect

```
agent proposes an action ─► tool-risk gate (7.5) ─► low risk, earned autonomy ─► runs
                                   │
                                   ▼ otherwise
                      decision card (idea 1)
        recommendation + facts checked (3) + past decisions (2) + conflicts (4)
                                   │
                           owner decides ─► verdict stored as a label
                                   │              │
                                   ▼              ▼
                       resume or cancel     agreement per action type (2)
                                            and calibration set (5.4)
```

## Cost

Each card is one or two Jev calls plus one short cheap-model call for the
explanation: well under a tenth of a cent. The morning ranking of 50 items is
50 Jev calls, about $0.001.
