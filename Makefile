.PHONY: check lint typecheck test fmt

check: lint typecheck test

lint:
	uv run ruff check src tests

typecheck:
	uv run ty check src tests

test:
	uv run pytest

fmt:
	uv run ruff format src tests
	uv run ruff check --fix --unsafe-fixes src tests
