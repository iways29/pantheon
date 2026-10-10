# ADR 040: What a tool made gets through when its words are withheld

- **Status:** Accepted (owner's go, 2026-10-10)
- **Date:** 2026-10-10
- **Step:** 11 follow-up

## Context

The owner asked the Chief of Staff for an image. Marketing's lead called
Higgsfield's `generate_image` three times (each approved, each paid). Every
reply was quarantined by the injection screen (ADR 025): Higgsfield answers
"job started, now call jobs_wait", which is text aimed at the AI caller and
looks exactly like an injection. The job id went with it, so the lead could
not fetch the result and reported a failure. The chat had no way to show an
image anyway.

## Decision

**The screen's verdict covers the words, not the ids and trusted links.**
From every MCP reply, whatever the screen says, two things are kept:

- **ids** (UUIDs): they are not words and cannot instruct anyone;
- **media**: `https` links whose host the owner trusts for that server
  (`mcp_servers.media_hosts`, set on the Tools screen, at most 10, audited).

The rest of a withheld reply stays withheld. Higgsfield's host is
`d8j0ntlcm91z4.cloudfront.net`.

**The order card shows the images**, collected from the tool calls anywhere
in the order's tree, so it does not depend on the agent pasting a link. A
`_min` copy beside a full-size file is used as the thumbnail; a click opens
the full image.

**Rejected:** trusting a server's text outright. It switches off the
injection check for a service that receives the owner's prompts and runs
paid jobs.

## Consequences

- One additive migration (`20261010120000_mcp_media_hosts.sql`).
- A trusted host's URL path is still text an agent reads; a host is trusted
  only if the owner says so.
- The Sign in button reads "Sign in again" when a server is connected, and a
  server's problems show on its own row.
