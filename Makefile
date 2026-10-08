NODE_BIN ?= $(CURDIR)/poc/phase0/.tools/node-v24.21.0-darwin-arm64/bin
export PATH := $(NODE_BIN):$(PATH)
export PLAYWRIGHT_BROWSERS_PATH ?= $(CURDIR)/poc/phase0/.cache/playwright
export npm_config_cache ?= $(CURDIR)/.cache/npm
export npm_config_update_notifier := false

.PHONY: check backend-check frontend-check frontend-build test-e2e db-migrate serve dev-ui
check: backend-check frontend-check

backend-check:
	.venv/bin/ruff check src tests scripts
	.venv/bin/ruff format --check src tests scripts
	.venv/bin/mypy src
	.venv/bin/pytest -q

frontend-check: frontend-build
	cd frontend && npm test

frontend-build:
	cd frontend && npm run build

test-e2e: frontend-build
	cd frontend && npm run test:e2e

db-migrate:
	.venv/bin/python -c 'from project_log.db import Database; Database().migrate()'

serve:
	.venv/bin/python -m project_log

dev-ui:
	cd frontend && npm run dev
