# Alvos espelham o CI. Ferramentas vem do grupo dev do uv (`uv sync`).
# Os testes de integracao sobem o MongoDB por testcontainers: precisam de Docker
# (no macOS com colima, tests/integracao/conftest.py aponta o DOCKER_HOST).
PY := uv run
GIT_SHA := $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
GIT_DATE := $(shell git show -s --format=%cI HEAD 2>/dev/null || echo unknown)
COMPOSE := GIT_SHA=$(GIT_SHA) GIT_DATE=$(GIT_DATE) docker compose

.PHONY: install lock-check lint format typecheck security lint-arch test test-unit \
	check audit smoke run compose-up compose-down compose-logs seed

install:
	uv sync --frozen

lock-check:
	uv lock --check

lint:
	$(PY) ruff check .
	$(PY) ruff format --check .

format:
	$(PY) ruff format .
	$(PY) ruff check --fix .

# Codigo e testes no modo strict: teste mal tipado esconde erro de contrato.
typecheck:
	$(PY) mypy src tests

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

check: lock-check lint lint-arch typecheck security test
	@echo "Todos os gates passaram"

# CVE nas dependencias de runtime: o mesmo conjunto do job pip-audit. O export
# leva hashes e o pip-audit roda com --disable-pip (o lock ja e o fechamento
# completo), sem o venv temporario cujo ensurepip falha no Python 3.14 do macOS.
audit:
	@mkdir -p reports
	uv export --frozen --no-emit-project --no-dev \
		--format requirements-txt -o reports/requirements-prod.txt
	uvx pip-audit==2.10.1 -r reports/requirements-prod.txt --strict \
		--disable-pip --progress-spinner off

# Smoke da imagem pelo entrypoint real (init do banco, seed, usuario 1001), o
# job build do CI: sobe a stack, confere a readiness (MongoDB preparado), que
# rota autenticada sem token responde 401 e que a resposta nao anuncia o
# servidor; derruba tudo com os volumes, inclusive em falha (depois de mostrar
# os logs). Projeto e portas proprios para nao derrubar a stack do compose-up.
SMOKE_PORT ?= 18002
SMOKE_MONGO_PORT ?= 17017
SMOKE_URL := http://127.0.0.1:$(SMOKE_PORT)
SMOKE_COMPOSE := API_PORT=$(SMOKE_PORT) MONGO_PORT=$(SMOKE_MONGO_PORT) \
	$(COMPOSE) -p pytstop-billing-smoke

smoke:
	@status=0; \
	$(SMOKE_COMPOSE) up -d --build --wait \
	&& curl -fsS --max-time 5 $(SMOKE_URL)/api/v1/saude/pronto && echo \
	&& codigo="$$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 $(SMOKE_URL)/api/v1/precos/servicos)" \
	&& test "$$codigo" = 401 \
	&& ! curl -sS -D - -o /dev/null --max-time 5 $(SMOKE_URL)/api/v1/saude | grep -qi '^server:' \
	&& echo "smoke ok: readiness 200, rota autenticada sem token 401, sem header server" \
	|| status=$$?; \
	if [ $$status -ne 0 ]; then $(SMOKE_COMPOSE) logs --no-color --tail=200; fi; \
	$(SMOKE_COMPOSE) down -v; \
	exit $$status

# API local apontando para o MongoDB do compose (make compose-up).
run:
	ENVIRONMENT=development $(PY) uvicorn src.main:criar_app --factory --reload --port 8002

compose-up:
	$(COMPOSE) up -d --build --wait

compose-down:
	$(COMPOSE) down -v

compose-logs:
	$(COMPOSE) logs -f api prazos

seed:
	$(COMPOSE) exec api python -m src.seed
