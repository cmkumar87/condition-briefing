.PHONY: install check test contract lint fmt fixtures network

# Owned by the contract owner. No stream edits this file.

install:
	uv sync --extra dev

# Every stream must leave `make check` green before handing work back.
check: lint contract test

lint:
	uv run ruff check app tests evals
	uv run ruff format --check app tests evals

fmt:
	uv run ruff format app tests evals
	uv run ruff check --fix app tests evals

# Contract tests. These are the convergence guarantee — they run against the
# shared fixtures and must pass in every worktree, on every stream.
contract:
	uv run pytest tests/contract -q

test:
	uv run pytest -q -m "not network and not billed"

# Hits live external APIs. Not part of `make check`.
network:
	uv run pytest -q -m network

# Regenerate fixtures from live APIs. Contract owner only — regenerating
# changes the bytes every stream tests against.
fixtures:
	uv run python tests/fixtures/_capture.py
