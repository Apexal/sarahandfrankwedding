.PHONY: help dev test lint lint-fix migrate seed setup css

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

setup: ## Initial project setup (install deps, copy env, migrate, seed, install CSS)
	uv sync
	@test -f .env || cp .env.example .env && echo "Created .env from .env.example"
	uv run ./manage.py migrate
	uv run ./manage.py seed
	uv run ./manage.py createcachetable
	uv run ./manage.py sync_groups
	mv .github/_workflows .github/workflows
	mkdir -p static/css
	cd static/css && curl -sL daisyui.com/fast | bash
	cd ../..
	uv run pre-commit install
	@echo ""
	@echo "Setup complete! Run 'make dev' to start the development server."

dev: ## Start the development server
	$(MAKE) css & uv run ./manage.py runserver

css: ## Watch and rebuild Tailwind CSS
	static/css/tailwindcss -i static/css/input.css -o static/css/output.css

css-watch: ## Watch and rebuild Tailwind CSS on changes
	static/css/tailwindcss -i static/css/input.css -o static/css/output.css --watch

test: ## Run tests
	uv run pytest

lint: ## Run linter checks
	uv run ruff check .
	uv run ruff format --check .

lint-fix: ## Auto-fix lint issues
	uv run ruff check --fix .
	uv run ruff format .

migrate: ## Create and run migrations
	uv run ./manage.py makemigrations
	uv run ./manage.py migrate
	uv run ./manage.py createcachetable
	uv run ./manage.py sync_groups

seed: ## Seed the database with sample data
	uv run ./manage.py seed
