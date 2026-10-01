.DEFAULT_GOAL := help

COMPOSE := docker compose

# Prefix for the commands that run Python tooling (videos, test, lint, fmt).
# Empty by default, so they use whichever environment is active — a venv made
# by `make sync`, by `make install-pip`, or your own. To run inside the
# uv-managed .venv without activating it: `make test RUN="uv run"`.
RUN ?=

.PHONY: help lock lock-check sync install install-pip videos test lint fmt up down logs ps build restart clean clean-volumes

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

lock: ## Re-resolve dependencies and rewrite uv.lock (commit the result)
	uv lock

lock-check: ## Fail if uv.lock is stale relative to pyproject.toml (what CI runs)
	uv lock --check

sync: ## Install exactly what uv.lock pins (all extras) into .venv, with uv
	uv sync --locked --extra ui --extra worker --extra test

install: sync ## Install the project from uv.lock (alias for sync)

install-pip: ## Without uv: pip install all extras (resolves fresh, ignores uv.lock)
	python -m pip install --upgrade pip
	pip install -e ".[ui,worker,test]"

videos: ## Download demo footage into data/videos/ (~65MB)
	$(RUN) python scripts/fetch_demo_videos.py

test: ## Run the test suite
	$(RUN) pytest

lint: ## Check formatting and lint rules (no changes made)
	$(RUN) ruff check .
	$(RUN) ruff format --check .

fmt: ## Auto-fix lint issues and reformat
	$(RUN) ruff check --fix .
	$(RUN) ruff format .

up: ## Build images if needed and start the stack in the background
	$(COMPOSE) up -d --build

down: ## Stop the stack and remove containers (named volumes are kept)
	$(COMPOSE) down

logs: ## Follow logs for all services
	$(COMPOSE) logs -f

ps: ## Show service status
	$(COMPOSE) ps

build: ## Build (or rebuild) images without starting anything
	$(COMPOSE) build

restart: ## Restart all services
	$(COMPOSE) restart

clean: down ## Same as down: remove containers and networks; volumes are KEPT

# `clean` used to run `down -v`, which silently deleted the Postgres history
# and the issued TLS certificate. Destroying data now needs its own target and an
# explicit CONFIRM=yes, and says what it is about to destroy.
clean-volumes: ## DESTRUCTIVE: also delete volumes (history, certs). Needs CONFIRM=yes
	@echo "This will DELETE every named volume of this stack:"
	@echo "  postgres-data  all stored crossing history (not recoverable without a dump)"
	@echo "  letsencrypt    the issued TLS certificate (re-issuing is rate-limited)"
	@echo "  model-weights  downloaded model weights (fetched again on the next start)"
	@if [ "$(CONFIRM)" != "yes" ]; then \
		echo "Refusing: nothing was deleted. Take a dump first (docs/DEPLOYMENT.md, Backups), then re-run with CONFIRM=yes"; \
		exit 1; \
	fi
	$(COMPOSE) down -v
