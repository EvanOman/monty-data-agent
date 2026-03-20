set shell := ["bash", "-cu"]

default:
    @just --list

fmt:
    uv run ruff format .

format-check:
    uv run ruff format --check .

lint:
    uv run ruff check .

lint-fix:
    uv run ruff check . --fix

type:
    uv run ty check . --exclude "src/sandbox_agent/engine/executor.py" --exclude "src/sandbox_agent/codemode/client.py"

test:
    uv run pytest

# FIX + CHECK: Run before every commit
fc: fmt lint-fix lint type test

ci: lint format-check type test

# Install dependencies
install:
    uv sync --dev

# Run the development server
serve:
    uv run uvicorn sandbox_agent.main:app --host 127.0.0.1 --port 19876 --reload --reload-dir src

# Start the Temporal dev server (requires Docker)
temporal-server:
    docker compose -f docker-compose.temporal.yml up -d

# Stop the Temporal dev server
temporal-down:
    docker compose -f docker-compose.temporal.yml down

# Run the Temporal worker (connects to Temporal server)
worker:
    uv run python -m sandbox_agent.temporal
