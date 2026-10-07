# Alvos espelham o CI. Ferramentas vem do grupo dev do uv (`uv sync`).
# Os testes de integracao sobem o MongoDB e o RabbitMQ por testcontainers:
# precisam de Docker (no macOS com colima, tests/integracao/conftest.py aponta o
# DOCKER_HOST). Os de contrato baixam o platform do GitHub (marcador rede).
PY := uv run
GIT_SHA := $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
GIT_DATE := $(shell git show -s --format=%cI HEAD 2>/dev/null || echo unknown)
COMPOSE := GIT_SHA=$(GIT_SHA) GIT_DATE=$(GIT_DATE) docker compose

.PHONY: install lock-check lint format typecheck security lint-arch test test-unit \
	check audit smoke manifests kind-deploy run compose-up compose-down compose-logs seed

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
# job build do CI: sobe a stack (o --wait espera o healthcheck de cada servico,
# inclusive relay e consumidor prontos, conectados ao RabbitMQ; servico doente
# derruba o --wait), publica um GerarOrcamento como o OS e espera o
# OrcamentoGerado (scripts/smoke_mensageria.py), confere a readiness da API
# (MongoDB preparado), que rota autenticada sem token responde 401 e que a
# resposta nao anuncia o servidor (os cabecalhos vem de uma variavel: num pipe,
# a falha do curl seria engolida e o smoke passaria sem ter olhado nada);
# derruba tudo com os volumes, inclusive em falha (depois de mostrar os logs).
# Projeto e portas proprios para nao derrubar a stack do compose-up nem a do
# platform.
SMOKE_PORT ?= 18002
SMOKE_MONGO_PORT ?= 17017
SMOKE_RABBITMQ_PORT ?= 15673
SMOKE_RABBITMQ_UI_PORT ?= 25673
SMOKE_URL := http://127.0.0.1:$(SMOKE_PORT)
# Usuario os do RabbitMQ do compose (senha de demonstracao do platform).
SMOKE_OS_URL := amqp://os:pytstop-os-demo-2026@rabbitmq:5672/%2F
SMOKE_COMPOSE := API_PORT=$(SMOKE_PORT) MONGO_PORT=$(SMOKE_MONGO_PORT) \
	RABBITMQ_PORT=$(SMOKE_RABBITMQ_PORT) RABBITMQ_UI_PORT=$(SMOKE_RABBITMQ_UI_PORT) \
	$(COMPOSE) -p pytstop-billing-smoke

smoke:
	@status=0; \
	$(SMOKE_COMPOSE) up -d --build --wait \
	&& echo "relay e consumidor prontos (healthcheck: conectados ao RabbitMQ)" \
	&& $(SMOKE_COMPOSE) exec -T -e SMOKE_OS_URL=$(SMOKE_OS_URL) relay \
		python - < scripts/smoke_mensageria.py \
	&& curl -fsS --max-time 5 $(SMOKE_URL)/api/v1/saude/pronto && echo \
	&& codigo="$$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 $(SMOKE_URL)/api/v1/precos/servicos)" \
	&& test "$$codigo" = 401 \
	&& cabecalhos="$$(curl -fsS -D - -o /dev/null --max-time 5 $(SMOKE_URL)/api/v1/saude)" \
	&& printf '%s\n' "$$cabecalhos" | grep -q '^HTTP/' \
	&& ! printf '%s\n' "$$cabecalhos" | grep -qi '^server:' \
	&& echo "smoke ok: readiness 200, rota autenticada sem token 401, sem header server" \
	|| status=$$?; \
	if [ $$status -ne 0 ]; then $(SMOKE_COMPOSE) logs --no-color --tail=200; fi; \
	$(SMOKE_COMPOSE) down -v; \
	exit $$status

# Manifests do Kubernetes (k8s/), os tres overlays: kubeconform com os schemas
# do Kubernetes 1.35, a versao do no do kind (Secret reprova: as senhas vem do
# make deploy do platform), e trivy config sem achado HIGH ou CRITICAL (o que
# ele ignora, com o motivo, esta em k8s/trivy-ignore.rego), nas versoes do
# platform. O job build do CI roda este alvo.
KUBERNETES_VERSION := 1.35.0
KUBECONFORM_IMAGE := ghcr.io/yannh/kubeconform:v0.8.0
TRIVY_IMAGE := aquasec/trivy:0.72.0

manifests:
	@mkdir -p reports
	@for overlay in kind kind-ci k3s; do \
		echo ">> k8s/overlays/$$overlay: kubeconform e trivy config"; \
		kubectl kustomize k8s/overlays/$$overlay > reports/k8s-$$overlay.yaml || exit 1; \
		docker run --rm -i $(KUBECONFORM_IMAGE) -strict -summary -output text \
			-kubernetes-version $(KUBERNETES_VERSION) -reject Secret - \
			< reports/k8s-$$overlay.yaml || exit 1; \
		docker run --rm -i -v "$(CURDIR)/k8s/trivy-ignore.rego:/trivy-ignore.rego:ro" \
			--entrypoint sh $(TRIVY_IMAGE) -c 'cat > /tmp/manifests.yaml && trivy config \
			--quiet --severity HIGH,CRITICAL --exit-code 1 \
			--ignore-policy /trivy-ignore.rego /tmp/manifests.yaml' \
			< reports/k8s-$$overlay.yaml || exit 1; \
	done

# Implanta o servico no kind da plataforma pelo script do CD do platform:
# constroi a imagem com o commit como tag, carrega no kind e aplica o overlay
# na ordem do contrato (Job de inicializacao, banco e rollouts). Antes, a
# plataforma no ar: make -C $(PLATFORM_DIR) kind-up deploy (README, Implantacao).
PLATFORM_DIR ?= ../postech-sw-arch-p4-platform
OVERLAY ?= kind

kind-deploy:
	$(PLATFORM_DIR)/scripts/ci/implantar-servicos.sh --overlay $(OVERLAY) \
		billing-service=$(CURDIR)

# API local apontando para o MongoDB do compose (make compose-up).
run:
	ENVIRONMENT=development $(PY) uvicorn src.main:criar_app --factory --reload --port 8002

compose-up:
	$(COMPOSE) up -d --build --wait

compose-down:
	$(COMPOSE) down -v

compose-logs:
	$(COMPOSE) logs -f api prazos relay consumidor

seed:
	$(COMPOSE) exec api python -m src.seed
