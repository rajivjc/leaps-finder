.PHONY: setup lint test scan refresh backtest web ci

VENV := scanner/.venv
PY := $(VENV)/bin/python

# Creates the 3.11 virtualenv the scanner needs and installs both apps.
setup:
	uv venv --python 3.11 $(VENV)
	uv pip install --python $(PY) -e "scanner[dev]"
	cd apps/web && npm install

lint:
	cd scanner && ../$(VENV)/bin/ruff check . && ../$(VENV)/bin/ruff format --check .
	cd apps/web && npm run lint && npx next typegen && npx tsc --noEmit

test:
	cd scanner && ../$(VENV)/bin/pytest
	cd apps/web && npm test

# The same job GitHub Actions runs on Saturdays — needs scanner/.env (see .env.example).
scan:
	cd scanner && ../$(VENV)/bin/python -m leaps_scanner.run_scan full

refresh:
	cd scanner && ../$(VENV)/bin/python -m leaps_scanner.run_scan refresh

# SPEC-BACKTEST.md's analysis: manual local runs only, never a cron job. The
# first run is a 30-45 min network-bound fetch; later ones read the local cache.
backtest:
	cd scanner && ../$(VENV)/bin/python -m leaps_scanner.backtest

web:
	cd apps/web && npm run dev

ci: lint test
	cd apps/web && npm run build
