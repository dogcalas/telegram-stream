.DEFAULT_GOAL := check

.PHONY: setup format format-check lint typecheck test check build

setup:
	uv sync --locked

format:
	uv run --locked ruff format .

format-check:
	uv run --locked ruff format --check .

lint:
	uv run --locked ruff check .

typecheck:
	uv run --locked ty check

test:
	uv run --locked pytest

check: format-check lint typecheck

build:
	uv build --no-sources
