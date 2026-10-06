# Alvos espelham o CI. Ferramentas vem do grupo dev do uv (`uv sync`).
# Os testes de integracao sobem o MongoDB por testcontainers: precisam de Docker
# (no macOS com colima, tests/conftest.py aponta o DOCKER_HOST sozinho).
PY := uv run
GIT_SHA := $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
GIT_DATE := $(shell git show -s --format=%cI HEAD 2>/dev/null || echo unknown)
COMPOSE := GIT_SHA=$(GIT_SHA) GIT_DATE=$(GIT_DATE) docker compose

.PHONY: install lint format typecheck security lint-arch test test-unit check \
	run compose-up compose-down compose-logs seed

install:
	uv sync --frozen

lint:
	$(PY) ruff check .
	$(PY) ruff format --check .

format:
	$(PY) ruff format .
	$(PY) ruff check --fix .

typecheck:
	$(PY) mypy src

security:
	$(PY) bandit -r src -q

lint-arch:
	$(PY) lint-imports

# Suite completa (unitarios + integracao em MongoDB real) com o gate de
# cobertura do .coveragerc; relatorios em coverage.xml, htmlcov/ e reports/.
test:
	@mkdir -p reports
	$(PY) pytest --cov --cov-report=term-missing --cov-report=xml:coverage.xml \
		--cov-report=html:htmlcov --junitxml=reports/junit.xml

# Sem Docker: so os unitarios, sem gate de cobertura.
test-unit:
	$(PY) pytest tests/unitarios -q

check: lint lint-arch typecheck security test
	@echo "Todos os gates passaram"

# API local apontando para o MongoDB do compose (make compose-up).
run:
	ENVIRONMENT=development $(PY) uvicorn src.main:criar_app --factory --reload --port 8002

compose-up:
	$(COMPOSE) up -d --build --wait

compose-down:
	$(COMPOSE) down

compose-logs:
	$(COMPOSE) logs -f api prazos

seed:
	$(COMPOSE) exec api python -m src.seed
