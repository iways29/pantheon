# Design brief: "The Brain", the live centre of Pantheon

What Pantheon is. A private company run by AI agents for one owner (the founder of The Unreal Lab). Agents are organised into departments; right now those are Research, the Executive Office and Marketing. Each department has a head and workers. At the centre sits the brain: a shared store of checked facts that agents read from and write to. Every action an agent takes is recorded as an event. The brain screen makes that activity visible and lets the owner step in.

The one feeling. A calm, living mind that the owner can see working. It glows and flows only when something really happens. Every movement comes from a real event, never decorative animation. At rest it is quiet and dim; when agents work it lights up where they work.

## What must be on the screen

1. The brain (centre). Facts shown as points of light. Related facts sit close together; closeness comes from meaning, and we have similarity data for every fact. A fact has a status:
   - active: steady glow;
   - disputed: flicker or warning colour;
   - superseded: faded, still visible as history;
   - held for the owner: marked as waiting.
   A newly admitted fact arrives visibly, as a pulse travelling from the agent that found it into the brain. A rejected fact shows briefly and dissolves.
2. Departments and agents (around the brain). Each department is a region or cluster; each agent is a node with its current state: idle, working, waiting for approval, paused, stopped. When an agent reads the brain, light flows out of the brain to it; when it writes, light flows in. When a head hands work to a worker, a thread travels between them.
3. Tasks in motion. The owner's orders flow from the Chief of Staff to a department, then split to workers, then come back as results. The owner can follow one order's path and see what the whole chain cost.
4. Needs-you signals. Anything waiting for the owner stands out without shouting:
   - an approval, which shows Jev's recommendation (approve, reject or look closer), the facts checked, and any clash with an earlier owner decision;
   - a question from the Chief of Staff;
   - a changed MCP tool that needs re-approval.
5. Always-visible controls: Pause (everything stops, resumable later) and Kill (cancels all unfinished work for good; needs a typed confirmation). The current state (running, paused) must be obvious at a glance.
6. The chat. A panel to talk to the Chief of Staff and give orders in plain words. Replies and questions appear there, and each order links to its path in the brain.
7. Today's pulse. A small, quiet strip showing: spend today against budget, facts added and rejected, tasks done, approvals waiting.

## Interactions

- Tap a fact: its claim, its source link, which agent found it, when, Jev's check, and which facts it contradicts or replaced.
- Tap an agent: what it is doing now, its last results, its tools, its autonomy level, today's spend.
- Tap an approval: the decision card with approve, edit, reject, or redirect with a note.
- Filter: by department, by time ("the last hour"), and "only what needs me".
- Replay: scrub back through the day and watch it happen again.

## The data behind it (real, so the design must fit it)

- facts: claim, source, status, when admitted, which document or page it came from, and neighbours by similarity;
- agents: department, role (head or worker), state;
- tasks: a tree with status and cost;
- approvals, with their decision cards;
- a live stream of events: "tool used"; "fact admitted / rejected / held"; "task started / waiting / done / failed"; "order routed"; "question for the owner"; "approval requested / decided"; "run paused"; "paused / killed".

Expect a few hundred to a few thousand facts at first. The design must stay readable at 10,000.

## Look and voice

The Unreal Lab: myth-coded, restrained, composed, few words chosen well; understated confidence, never hype. Dark by default, with a light mode. Mythological themes are welcome in the look. All words on screen are in English, with no Sanskrit in the interface. Light and motion carry meaning; colour is used sparingly for status. It must be accessible: status never shown by colour alone, and a reduced-motion setting.

## Platforms

Desktop is the main working view. The phone version must still show the pulse, what needs you, pause and kill, and the chat.

## Added later by the owner

- The brain inside the circle must be an infinite, movable and zoomable canvas, to cope with a growing brain.
- Agents will grow too: departments must cluster, and open or close.
- Use Liquid Glass throughout, including the lens.
