.PHONY: install lint format test test-db harness migrate migration-preview dev-up up down logs ps clean

PYTHON ?= python3
VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

install:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e 'backend[dev]'

lint:
	$(VENV)/bin/ruff check backend
	$(VENV)/bin/ruff format --check backend
	PYTHONPATH=backend $(VENV)/bin/mypy --config-file backend/pyproject.toml backend/app backend/migrations

format:
	$(VENV)/bin/ruff check --fix backend
	$(VENV)/bin/ruff format backend

test:
	PYTHONPATH=backend $(PY) -m pytest backend/tests

test-db:
	PYTHONPATH=backend $(PY) -m pytest backend/tests/unit/test_db_contract.py

harness:
	cd backend && ../$(PY) scripts/run_harness_ci.py --mock --min-cases 180 --fail-below 0.94

migrate:
	cd backend && ../$(PY) -m migrations.migrate

migration-preview:
	@for f in backend/migrations/[0-9][0-9][0-9]_*.sql; do echo "-- $$f"; printf '%s\n' '----------------'; cat "$$f"; done

dev-up up:
	docker compose --env-file .env up -d --build

down:
	docker compose --env-file .env down

logs:
	docker compose --env-file .env logs -f --tail=100

ps:
	docker compose --env-file .env ps

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache backend/.pytest_cache backend/.coverage
