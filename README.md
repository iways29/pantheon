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
# API — http://localhost:8000
cd api
uv sync --group dev
uv run fastapi dev app/main.py --port 8000

# Web — http://localhost:3000
cd web
pnpm install
pnpm dev
```

## Checks

These are what CI runs.

```bash
cd api && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
cd web && pnpm typecheck && pnpm build
```

## Owner prerequisites

Some of Step 0 cannot be finished from the repository alone. See the
"Owner prerequisites" section of `docs/BUILD_PLAN.md`; at minimum a Supabase
project and the two Vercel projects must exist before the app can deploy and
the owner can log in.
