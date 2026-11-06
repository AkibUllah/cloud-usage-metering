PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
TEST_DATABASE_URL ?= postgresql+psycopg://metering:metering@localhost:5432/metering

.PHONY: venv install dev test test-postgres lint fmt run openapi docker-up docker-down clean simulate

venv:
	$(PY) -m venv $(VENV)

install: venv
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

dev: install

run:
	$(BIN)/uvicorn metering.asgi:app --reload --port 8000

test:
	$(BIN)/pytest

# Runs the suite on PostgreSQL; the database must be a throwaway one, as rows are deleted.
test-postgres:
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(BIN)/pytest

lint:
	$(BIN)/ruff check src tests

fmt:
	$(BIN)/ruff format src tests
	$(BIN)/ruff check --fix src tests

openapi:
	$(BIN)/python scripts/export_openapi.py

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down -v

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info src/*.egg-info
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +

simulate:
	$(BIN)/python scripts/simulate_usage.py --base-url http://localhost:8000 --days 3
