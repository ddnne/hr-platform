.PHONY: setup test demo check worker-test
setup:
	uv sync --frozen --python 3.12
	npm ci
check:
	uv run --frozen ruff check src tests scripts
	npm run typecheck
test:
	uv run --frozen pytest -q
	$(MAKE) check
	$(MAKE) worker-test
worker-test:
	npm test
demo:
	uv run --frozen python -m hr_platform.demo
