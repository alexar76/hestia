.PHONY: test run lint

test:
	uv sync --extra dev --project .
	uv run --project . ruff check .
	uv run --project . pytest -q

run:
	uv run --project . python -m hestia

lint:
	uv run --project . ruff check .
