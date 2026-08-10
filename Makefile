.DEFAULT_GOAL := help

COMPOSE := docker compose

.PHONY: help install videos test lint fmt up down logs ps build restart clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Install project dependencies with Poetry
	poetry install

videos: ## Download demo footage into data/videos/ (~65MB)
	poetry run python scripts/fetch_demo_videos.py

test: ## Run the test suite
	poetry run pytest

lint: ## Check formatting and lint rules (no changes made)
	poetry run ruff check .
	poetry run ruff format --check .

fmt: ## Auto-fix lint issues and reformat
	poetry run ruff check --fix .
	poetry run ruff format .

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

clean: ## Stop the stack and remove containers, networks, and volumes
	$(COMPOSE) down -v
