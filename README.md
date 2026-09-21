# Pantheon

An AI-agent "employee" system with a **brain** at its center: a shared fact
house that agents read from and write to.

- `CLAUDE.md` — the rules that apply to every change. Read it first.
- `docs/BUILD_PLAN.md` — the ordered build steps and acceptance criteria.
- `docs/adr/` — architectural decisions, one page each.

## Layout

| Path                  | What                                             |
| --------------------- | ------------------------------------------------ |
| `web/`                | Next.js frontend, TypeScript strict              |
| `api/`                | FastAPI backend, deployed as Vercel Functions    |
| `supabase/migrations/`| Versioned SQL; RLS on every table                |

Deployment shape and its rationale: `docs/adr/0001-monorepo-and-deployment-layout.md`.

## Local development

Copy `.env.example` to `.env` and fill it in. Secrets are server-side only and
`.env` is never committed.

```bash
make install     # API and web dependencies
make api-dev     # http://localhost:8000
make web-dev     # http://localhost:3000
make check       # everything CI runs
```

`make help` lists every target.

## Database

The supported path is the Supabase CLI, which runs Postgres, Auth and Realtime
locally and matches production most closely. It requires Docker.

```bash
supabase start      # bring up the local stack
supabase db reset   # re-apply every migration from scratch
```

Set `major_version` in `supabase/config.toml` to match your hosted project
before relying on it — run `SHOW server_version;` there to check. The CLI
default may not match.

Where Docker is unavailable, `scripts/local_db.sh` applies the same migrations
to a plain PostgreSQL server with pgvector. It is a testing convenience with no
Auth, Realtime or Storage:

```bash
make db-reset    # drop, recreate, re-apply all migrations
make db-psql     # interactive shell
```

Migrations are additive and versioned in `supabase/migrations/`. Never edit a
migration that has been applied; add a new one.

## Checks

`make check` runs all of these, and they are what CI runs.

```bash
cd api && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
cd web && pnpm typecheck && pnpm build
```

## Owner prerequisites

Some of Step 0 cannot be finished from the repository alone. See the
"Owner prerequisites" section of `docs/BUILD_PLAN.md`; at minimum a Supabase
project and the two Vercel projects must exist before the app can deploy and
the owner can log in.
