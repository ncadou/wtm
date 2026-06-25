.PHONY: reformat lint check test

# Apply safe autofixes (import sorting, pyupgrade, dead-import removal) + format.
reformat:
	uv run ruff check --fix src tests
	uv run ruff format src tests

# Lint: pyflakes, pycodestyle, isort, pyupgrade, bugbear, comprehensions.
lint:
	uv run ruff check src tests

# Verify format + lint without writing (CI-style gate).
check:
	uv run ruff format --check src tests
	uv run ruff check src tests

# Run the test suite.
test:
	uv run pytest
