NODE_BIN := $(CURDIR)/.tools/node-v24.21.0-darwin-arm64/bin
export PATH := $(NODE_BIN):$(PATH)
export PLAYWRIGHT_BROWSERS_PATH ?= $(CURDIR)/.cache/playwright
export npm_config_cache ?= $(CURDIR)/.cache/npm
export npm_config_update_notifier := false

.PHONY: check backend-check frontend-check frontend-build serve dev-ui test-e2e
check: backend-check frontend-check

backend-check:
	.venv/bin/ruff check src tests scripts
	.venv/bin/ruff format --check src tests scripts
	.venv/bin/mypy src
	.venv/bin/pytest -q

frontend-check: frontend-build
	cd frontend && npm run test

frontend-build:
	cd frontend && npm run build

serve:
	.venv/bin/python -m project_log

dev-ui:
	cd frontend && npm run dev

test-e2e: frontend-build
	cd frontend && npm run test:e2e
