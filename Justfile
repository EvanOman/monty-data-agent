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
    uv run ty check . --exclude "src/sandbox_agent/sandbox/executor.py"

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
