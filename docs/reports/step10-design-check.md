# Step 10: the brain screen against the designs

Checked 2026-09-28 in `/preview` (sample data) at 1440 × 900, and at phone
size (375 × 812), against `docs/design/pantheon-design/screens/*.jpg`. The
live canvas (https://claude.ai/artifact/QSHVv8Xjd9ZiSuXvEKhCZ1) holds the same
files (Main, Field, ScaleNotes and Events compared byte for byte).

## Fixed while checking

| Screen | What was off | Now |
| --- | --- | --- |
| Phone | Pause and Kill stacked full width | Side by side, as designed |
| Phone | Globe kept room for agent names the phone hides, so it was small | About twice the size |
| Phone | Neighbourhood names and the zoom pill overlapped the ball and the fact count | Left off, as designed (pinch zooms) |
| Light | Gold text 4.3:1 on the page (below 4.5:1) | 5.1:1; the globe's points keep the design's gold |
| Main | Key hidden under the folded Needs you button | Key sits above it |

## Matches

| Screen | Notes |
| --- | --- |
| Main | Layout, panels, dial with the hour hand and today's ticks, agent ring, open and closed department hubs, minimap, pulse strip, "Replay the day" |
| Paused | Veil wording, Resume button, "Held at the last event", seats marked paused |
| Killed | Veil, "All work is stopped", seats as squares, "Held at the last event" |
| Events | Threads for reads and writes, glow fading over 90 s, hand-offs as ice threads, reduced motion without travelling beads |
| ScaleNotes | Rim chevrons for activity out of view; the view moves only when asked ("Follow this order", a rim chevron); closed departments' threads end at the hub |

## Different on purpose

| Design | Built | Why |
| --- | --- | --- |
| Flat lens | 3D glass globe | Owner's choice (ADR 036) |
| Filter chips in the header | One "Filters" menu | Room for the Control Center tab; same filters |
| Key open in two corners | Folded "Key" button | Keeps the ring clear (earlier review) |
| "− Facts +" zoom pill | "− Drag to turn +" | The globe turns as well as zooms |
| Killed: "give the Chief of Staff a new order" | Adds "Lift the pause" | The kill leaves the pause on (ADR 023); starting again needs it lifted |

## Small differences left (not fixed)

| Where | Design | Built |
| --- | --- | --- |
| Chat, paused or killed | A system line: "You paused everything at 14:36. Nothing is lost." | No line; the veil and the header say it |
| Pulse strip, killed | "14 done, 5 cancelled" | "14 done" (cancelled not counted) |
| Light theme | Points glow warmly | Points read darker at small sizes (same colour, normal blending) |
| Preview only | Killed shows "none" in Needs you | The sample data keeps three items; live, the kill expires them |
