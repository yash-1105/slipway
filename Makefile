# Slipway
#
# Every target runs from the repository root. Nothing here hardcodes a host, a
# port or an absolute path; configuration comes from the environment.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

ORCH := orchestrator
UV := uv --project $(ORCH)
RUN := $(UV) run
# ruff, mypy and pytest all resolve their configured paths relative to the
# working directory, so they run from inside the orchestrator package rather
# than from the repository root.
IN_ORCH := cd $(ORCH) && uv run
COMPOSE := docker compose --file deploy/compose.yaml

# Local development defaults. Override any of these in your own environment;
# they exist so `make dev` works on a fresh checkout, not as production values.
export POSTGRES_PASSWORD ?= slipway-local
# 5433, not 5432: a system Postgres is commonly already on 5432 and the
# container would fail to bind. Override if that is not true for you.
export POSTGRES_PORT     ?= 5433
export SLIPWAY_DATABASE_URL ?= postgresql+asyncpg://slipway:$(POSTGRES_PASSWORD)@127.0.0.1:$(POSTGRES_PORT)/slipway

# Seam selection on a laptop. Each real backend turns itself on as soon as the
# credential it needs is present, so nothing has to be remembered: export a
# Novita key and the models seam is Novita, export a deploy host and the deploy
# seam is compose-over-SSH. Without them, a fresh checkout still boots.
ifeq ($(strip $(SLIPWAY_NOVITA_API_KEY)),)
export SLIPWAY_MODELS_BACKEND ?= fake
endif
ifeq ($(strip $(SLIPWAY_DEPLOY_SSH_HOST)),)
export SLIPWAY_DEPLOY_BACKEND ?= fake
export SLIPWAY_DEPLOY_PUBLIC_HOST ?= 127.0.0.1
endif

.PHONY: help
help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# development
# ---------------------------------------------------------------------------

.PHONY: install
install:  ## Install Python and frontend dependencies
	$(UV) sync --group dev
	cd frontend && npm install

.PHONY: dev
dev: db migrate  ## Run Postgres, the API, the worker and the frontend
	@echo "api      http://$${SLIPWAY_API_HOST:-127.0.0.1}:$${SLIPWAY_API_PORT:-8000}"
	@echo "frontend http://127.0.0.1:5173"
	@trap 'kill 0' EXIT INT TERM; \
	(cd $(ORCH) && uv run python -m app.main) & \
	(cd $(ORCH) && uv run python -m app.worker) & \
	(cd frontend && npm run dev) & \
	wait

.PHONY: db
db:  ## Bring up only the local Postgres
	$(COMPOSE) up --detach --wait postgres

.PHONY: db-stop
db-stop:  ## Stop the local Postgres, keeping its data
	$(COMPOSE) stop postgres

# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

.PHONY: check
check: lint types imports env-access test-unit  ## Everything that must pass before a commit

.PHONY: lint
lint:  ## ruff
	$(IN_ORCH) ruff check .

.PHONY: format
format:  ## ruff --fix
	$(IN_ORCH) ruff check --fix .
	$(IN_ORCH) ruff format .

.PHONY: types
types:  ## mypy strict
	$(IN_ORCH) mypy

.PHONY: imports
imports:  ## import-linter: the layering and the five seams (ADRs 0001, 0002)
	$(IN_ORCH) lint-imports

.PHONY: env-access
env-access:  ## Fail if anything outside app/config.py reads the environment
	$(RUN) python scripts/check_env_access.py

.PHONY: test-unit
test-unit:  ## Unit tests: pure logic, no IO
	$(IN_ORCH) pytest tests/unit -q

.PHONY: test-integration
test-integration: db migrate  ## Integration tests: real Postgres, real Docker
	$(IN_ORCH) pytest tests/integration -q

.PHONY: test-acceptance
test-acceptance: db migrate  ## Acceptance tests: the full loop
	$(IN_ORCH) pytest tests/acceptance -q

.PHONY: test
test: test-unit test-integration test-acceptance  ## Every test

# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

.PHONY: migrate
migrate:  ## Apply pending forward-only migrations
	$(IN_ORCH) python -m app.cli.main migrate

.PHONY: seed
seed:  ## Insert local development fixtures (never any secret)
	$(IN_ORCH) python ../scripts/seed.py

.PHONY: reset
reset:  ## Drop and recreate the local database, then migrate
	@printf 'This destroys the local Slipway database. Continue? [y/N] ' && read -r reply && [[ $$reply == [yY] ]]
	$(COMPOSE) down --volumes postgres
	$(MAKE) db migrate

# ---------------------------------------------------------------------------
# models and evals
# ---------------------------------------------------------------------------

.PHONY: models-sync
models-sync:  ## Regenerate config/models.yaml from the live /models endpoint
	$(IN_ORCH) python ../scripts/sync_models.py

.PHONY: eval
eval:  ## Run the eval cases in evals/cases
	$(IN_ORCH) python ../scripts/run_evals.py ../evals/cases

# ---------------------------------------------------------------------------
# deployment
# ---------------------------------------------------------------------------

.PHONY: deploy
deploy:  ## Deploy the orchestrator to the V1 server
	deploy/deploy.sh

.PHONY: sandbox-image
sandbox-image:  ## Build the agent sandbox image
	docker build --tag $${SLIPWAY_SANDBOX_IMAGE:-slipway/sandbox:dev} sandbox-image
