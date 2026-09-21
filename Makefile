# Pantheon developer commands. See README.md for setup.
.PHONY: help install check api-test api-lint web-check api-dev web-dev db-create db-reset db-psql

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install API and web dependencies
	cd api && uv sync --group dev
	cd web && pnpm install

check: api-lint api-test web-check ## Run everything CI runs

api-lint: ## Lint and format-check the API
	cd api && uv run ruff check . && uv run ruff format --check .

api-test: ## Run API tests
	cd api && uv run pytest -q

web-check: ## Typecheck and build the frontend
	cd web && pnpm typecheck && pnpm build

api-dev: ## Run the API at http://localhost:8000
	cd api && uv run fastapi dev app/main.py --port 8000

web-dev: ## Run the frontend at http://localhost:3000
	cd web && pnpm dev

db-create: ## Create the local database and enable extensions
	./scripts/local_db.sh create

db-reset: ## Drop, recreate, and re-apply all migrations
	./scripts/local_db.sh reset

db-psql: ## Open a psql shell on the local database
	./scripts/local_db.sh psql
