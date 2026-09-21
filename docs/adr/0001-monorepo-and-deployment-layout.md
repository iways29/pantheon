# ADR 001: Monorepo layout and Vercel deployment shape

- **Status:** Accepted
- **Date:** 2026-09-20
- **Step:** 0 (Foundations)

## Context

Pantheon needs a Next.js frontend and a Python FastAPI backend in one
repository, both deployed on Vercel. `docs/BUILD_PLAN.md` Step 0 requires us to
verify the current recommended way to do that rather than assume, and to record
the chosen layout here.

The owner does not have a Vercel Pro plan yet, so the layout must not depend on
Pro-only features to work.

## What the current Vercel documentation says

Checked against the live Vercel documentation. Note that `vercel.com` is
unreachable from the build agent's network, so these come from Vercel's
documentation search rather than the rendered pages; the per-plan limit tables
in particular could not be read and are flagged as unverified below.

- FastAPI is a **first-class framework preset**. The backend is one ASGI
  application, not a directory of per-file Python functions. Vercel imports the
  app named by `[tool.vercel] entrypoint` in `pyproject.toml`.
- The **Python version** is pinned by a `.python-version` file or by
  `requires-python` in `pyproject.toml`. 3.12 and 3.13 are both selectable.
- **Dependencies** come from `pyproject.toml` (uv) or `requirements.txt`.
- **Fluid compute** is enabled with `{"fluid": true}` in `vercel.json`.
- **Per-function limits** are set under `functions`, keyed by the entrypoint
  path, e.g. `{"functions": {"app/main.py": {"maxDuration": 60}}}`.
  `excludeFiles` trims the deployed bundle to stay under the size limit, which
  matters for us because LangGraph, LangChain and database clients are large.
- Vercel now also supports **multiple services in a single project**: a
  `services` key in `vercel.json` gives each service its own `root`,
  `framework` and `entrypoint`, with `rewrites` routing to a named service and
  `bindings` injecting a sibling service's URL as an environment variable.

## Decision

**Repository layout**

```
web/                  Next.js, TypeScript strict   (package: pantheon-web)
api/                  FastAPI                      (package: pantheon-api)
supabase/migrations/  versioned SQL, RLS on every table
docs/adr/             these records
```

**Deployment shape: two Vercel projects from one repository.**

- `pantheon-web` with Root Directory `web/`
- `pantheon-api` with Root Directory `api/`, framework preset FastAPI

The frontend holds the Supabase session and calls the API with the access token
in an `Authorization: Bearer` header. The API verifies the token locally and
keeps a CORS allowlist of permitted browser origins.

## Why this shape

1. **Nothing about it is plan-gated.** It behaves identically on Hobby and on
   Pro, so Step 0 is not blocked on the owner upgrading. Pro bills per seat
   rather than per project, so a second project adds no cost later either.
2. **It is the long-established documented path**, which matters more than
   elegance while the rest of the stack is still unbuilt.
3. **The two are deployed and configured independently.** The Python bundle is
   the one at risk of hitting size and duration limits; keeping it in its own
   project means its limits, region and Fluid settings are tuned without
   touching the frontend.

## Alternative considered: one project with `services`

A single project declaring both services, rewriting `/api/*` to FastAPI and
`/*` to Next.js, is genuinely attractive: one origin removes CORS entirely,
removes the need to wire preview deployments to each other, and `bindings`
hands the frontend the backend URL automatically. It is clearly the direction
Vercel is moving.

It was not chosen **only** because its availability could not be confirmed from
this environment — neither its release status nor which plans offer it — and
committing the owner to an unverified feature before they have even chosen a
plan is the wrong trade at Step 0.

This is a cheap decision to revisit. The `web/` and `api/` directory split is
identical under both shapes, so migrating is a `vercel.json` change plus
deleting a project, not a restructure. Two things in the code already
anticipate it:

- the API reads `API_ROOT_PATH`, so serving under an `/api` prefix is a
  configuration change rather than a rewrite of every route, and
- the frontend reads `NEXT_PUBLIC_API_BASE_URL`, which becomes a relative path
  under a single origin.

Revisit once the owner is on a plan and the feature's availability can be
confirmed.

## Consequences

- The frontend must send bearer tokens rather than relying on same-origin
  cookies reaching the API, and `CORS_ALLOW_ORIGINS` must list every origin
  that calls the API, including preview deployments.
- Preview deployments of `web` need to be pointed at the matching preview of
  `api`. Vercel's `relatedProjects` in `vercel.json` exposes a sibling
  project's host as an environment variable and is the intended fix; it needs
  the real project IDs, so it is deliberately not committed with placeholder
  values. Wire it up once both projects exist.
- Two sets of environment variables to keep in step. `.env.example` is the
  single source of truth for their names.

## Confirmed in production (2026-09-21)

Step 0 deployed and its acceptance criteria were met against live services,
which settles two things this ADR had left open.

- **Supabase issues asymmetric JWTs, and the JWKS path is the one in use.**
  `SUPABASE_JWT_SECRET` is deliberately unset; the API verifies tokens against
  the JWKS endpoint derived from `SUPABASE_URL`, and a real owner session
  returned 200 from the protected route. The HS256 branch stays supported but
  is not the production path.
- **The two-project shape works as described.** `pantheon-web` (root `web/`)
  and `pantheon-api` (root `api/`) deploy independently from `main`, with the
  frontend passing the session token as a bearer header across origins.

One deployment detail worth recording, because it is not obvious: Vercel's
Deployment Protection is on by default and blocks the frontend's server-side
call to the API, since that request is external and arrives without a Vercel
session. It has to be disabled on `pantheon-api`. The real gate is the
Supabase token plus the owner check, not Vercel's auth wall.

## Decisions and open items

- **Function and database regions differ. Accepted for phase 1.** Functions
  run in `iad1` (Virginia); the Supabase project is in `us-west-2` (Oregon),
  so each query makes a cross-country round trip of roughly 60-80ms. The build
  plan's prerequisites ask for the regions to be close, and moving the project
  was cheap while it held no data. The owner weighed that against the cost of
  recreating it and chose to keep `us-west-2`. At phase-1 volume the cost is
  small; revisit if it shows up in per-run latency once agents issue many
  sequential queries per run, from Step 3 onward.
- `CLAUDE.md` states function duration limits of "default 300s, Pro max 800s,
  1800s beta". A `maxDuration` of 1800 does appear in Vercel's Python examples,
  but the per-plan table could not be read from this environment, and **Hobby
  limits are lower than Pro**. Confirm the ceiling before Step 3 sizes its
  runs, since "runs are short and resumable" depends on it.
