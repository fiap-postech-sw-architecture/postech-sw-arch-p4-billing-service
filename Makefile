# Alvos espelham o CI. Ferramentas vem do grupo dev do uv (`uv sync`).
# Os testes de integracao sobem o MongoDB e o RabbitMQ por testcontainers:
# precisam de Docker (no macOS com colima, tests/integracao/conftest.py aponta o
# DOCKER_HOST). Os de contrato baixam o platform do GitHub (marcador rede).
PY := uv run
GIT_SHA := $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
GIT_DATE := $(shell git show -s --format=%cI HEAD 2>/dev/null || echo unknown)
COMPOSE := GIT_SHA=$(GIT_SHA) GIT_DATE=$(GIT_DATE) docker compose

.PHONY: install lock-check lint format typecheck security lint-arch test test-unit \
	diff-cover mutacao check audit smoke manifests print-trivy-version kind-deploy run compose-up compose-down compose-logs seed \
	docs-api cobertura-readme

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

# Cobertura do codigo novo (ADR-041): as linhas e os ramos que a branch muda
# em relacao a origin/main, medidos no coverage.xml do `make test`, nao podem
# ficar abaixo de 90%. O gate de 95% do .coveragerc mede o total; este impede
# que uma mudanca sem teste passe so porque o total absorveu o codigo novo.
# Roda depois do `make test` e deixa o relatorio em reports/diff-cover.md (o
# CI o copia para o summary do job test). COMPARAR_COM troca a base. Com o
# coverage.xml mais velho que o codigo o diff-cover so veria "No lines with
# coverage information" e passaria em branco, entao o alvo recusa esse caso, e
# o do XML ausente, que viraria traceback do diff-cover.
# --include-untracked mede tambem o modulo novo que ainda nao foi para o git.
DIFF_COVER_VERSION := 10.6.0
DIFF_COVER_MINIMO ?= 90
COMPARAR_COM ?= origin/main

diff-cover:
	@mkdir -p reports
	@test -f coverage.xml \
		|| { echo "diff-cover: coverage.xml missing, run make test first" >&2; exit 1; }
	@test -z "$$(find src -name '*.py' -newer coverage.xml 2>/dev/null)" \
		|| { echo "diff-cover: coverage.xml is older than src/, run make test first" >&2; exit 1; }
	uvx diff-cover==$(DIFF_COVER_VERSION) coverage.xml --branch-coverage --include-untracked \
		--compare-branch=$(COMPARAR_COM) --fail-under=$(DIFF_COVER_MINIMO) \
		--format markdown:reports/diff-cover.md

# Mutacao nos modulos criticos (consumidor, relay, saga, status do orcamento e do
# pagamento, idempotencia, Dinheiro e JWT), sem gate e sem pontuacao minima. O
# mutmut nao roda aqui (o pacote se chama `src`, e ele exige o pacote fora de
# `src.`), entao sao mutantes escritos a mao em scripts/mutantes.toml, aplicados
# um por vez numa copia do codigo (mutants/); o relatorio sai em
# reports/mutacao.md. Para quando passa de MUTACAO_PRAZO segundos (15 min); os
# testes de integracao sobem um MongoDB cada, a menos que TEST_MONGODB_URI aponte
# para um replica set no ar. MUTANTES="D01 R03" roda so esses.
MUTACAO_PRAZO ?= 900
MUTANTES ?=

mutacao:
	$(PY) python scripts/mutacao_manual.py --prazo $(MUTACAO_PRAZO) $(if $(MUTANTES),--so $(MUTANTES))

# OpenAPI do app (o mesmo do /docs) e colecao Postman com um exemplo por rota,
# gerados juntos em docs/api para nao divergirem. So monta o app: nao sobe
# servidor nem banco. O make test reprova se os arquivos versionados ficarem
# atras do codigo (tests/unitarios/test_documentacao.py).
docs-api:
	$(PY) python scripts/gerar_docs_api.py

# A secao Cobertura do README sai do coverage.xml de um run do CI (o artefato
# relatorios-de-teste do job test), com o link dele, e nunca e digitada: o make
# test reprova a tabela editada a mao. RUN=<id do run> (gh run list --workflow
# ci.yml); o gh baixa o artefato em reports/ci-<id>. O RUN vai ao shell pelo
# ambiente ("$RUN" na receita), e nao colado no texto do comando: aspas ou crase
# nele rodariam antes de o case recusar o valor (que tambem recusa a quebra de linha).
# Uma funcao do make (RUN='$(shell ...)') rodaria ao exportar a variavel: o $(value)
# a guarda como texto.
CI_RUNS := https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service/actions/runs
override RUN := $(value RUN)
export RUN

cobertura-readme:
	@case "$$RUN" in ''|*[!0-9]*) \
		echo "cobertura-readme: RUN=<CI run id, digits only> is required" >&2; exit 2;; esac
	rm -rf "reports/ci-$$RUN"
	gh run download "$$RUN" -n relatorios-de-teste -D "reports/ci-$$RUN"
	$(PY) python scripts/cobertura_resumo.py "reports/ci-$$RUN/coverage.xml" \
		--readme README.md --run "$(CI_RUNS)/$$RUN"

check: lock-check lint lint-arch typecheck security test diff-cover
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
# OrcamentoGerado (scripts/smoke_mensageria.py), confere que o /metrics de cada
# um dos quatro processos lista as series do catalogo que sao dele, as de rotulo
# fechado em zero e as do fluxo que acabou de rodar em 1
# (scripts/smoke_metricas.py), confere a readiness da API
# (MongoDB preparado), que rota autenticada sem token responde 401, que a
# resposta nao anuncia o servidor (os cabecalhos vem de uma variavel: num pipe,
# a falha do curl seria engolida e o smoke passaria sem ter olhado nada), que a
# API roda como o usuario 1001, que as linhas de boot do uvicorn saem em JSON e
# que ele nao escreve access log (so o `http_request` estruturado do
# middleware), que o boot nao traz o aviso da telemetria automatica do FastAPI
# (o servico api do compose leva o endpoint OTLP no ambiente, como o cluster) e
# que a imagem, com ENVIRONMENT=production, recusa o segredo de demonstracao do
# compose (o compose sobe com development); derruba tudo com os volumes,
# inclusive em falha (depois de mostrar os logs).
# Projeto e portas proprios para nao derrubar a stack do compose-up nem a do
# platform.
SMOKE_PORT ?= 18002
SMOKE_MONGO_PORT ?= 17017
SMOKE_RABBITMQ_PORT ?= 15673
SMOKE_RABBITMQ_UI_PORT ?= 25673
SMOKE_URL := http://127.0.0.1:$(SMOKE_PORT)
# Usuario os do RabbitMQ do compose (senha de demonstracao do platform).
SMOKE_OS_URL := amqp://os:pytstop-os-demo-2026@rabbitmq:5672/%2F
# A guarda de producao da configuracao da API: o segredo do link de
# demonstracao so vale em development e test.
SMOKE_GUARDA := from src.configuracao import Configuracao; Configuracao.do_ambiente()
SMOKE_COMPOSE := API_PORT=$(SMOKE_PORT) MONGO_PORT=$(SMOKE_MONGO_PORT) \
	RABBITMQ_PORT=$(SMOKE_RABBITMQ_PORT) RABBITMQ_UI_PORT=$(SMOKE_RABBITMQ_UI_PORT) \
	$(COMPOSE) -p pytstop-billing-smoke

smoke:
	@status=0; \
	$(SMOKE_COMPOSE) up -d --build --wait \
	&& echo "relay e consumidor prontos (healthcheck: conectados ao RabbitMQ)" \
	&& $(SMOKE_COMPOSE) exec -T -e SMOKE_OS_URL=$(SMOKE_OS_URL) relay \
		python - < scripts/smoke_mensageria.py \
	&& $(SMOKE_COMPOSE) exec -T api \
		python - api http://127.0.0.1:8000/metrics < scripts/smoke_metricas.py \
	&& $(SMOKE_COMPOSE) exec -T relay \
		python - relay http://127.0.0.1:9100/metrics < scripts/smoke_metricas.py \
	&& $(SMOKE_COMPOSE) exec -T consumidor \
		python - consumidor http://127.0.0.1:9100/metrics < scripts/smoke_metricas.py \
	&& $(SMOKE_COMPOSE) exec -T prazos \
		python - prazos http://127.0.0.1:9100/metrics < scripts/smoke_metricas.py \
	&& curl -fsS --max-time 5 $(SMOKE_URL)/api/v1/saude/pronto && echo \
	&& codigo="$$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 $(SMOKE_URL)/api/v1/precos/servicos)" \
	&& test "$$codigo" = 401 \
	&& cabecalhos="$$(curl -fsS -D - -o /dev/null --max-time 5 $(SMOKE_URL)/api/v1/saude)" \
	&& printf '%s\n' "$$cabecalhos" | grep -q '^HTTP/' \
	&& ! printf '%s\n' "$$cabecalhos" | grep -qi '^server:' \
	&& { test "$$($(SMOKE_COMPOSE) exec -T api id -u)" = 1001 \
		|| { echo "smoke: a API nao roda como o usuario 1001" >&2; false; }; } \
	&& logs="$$($(SMOKE_COMPOSE) logs --no-color api)" \
	&& { printf '%s\n' "$$logs" | grep -q '"event": "Started server process' \
		|| { echo "smoke: as linhas de boot do uvicorn nao saem em JSON" >&2; false; }; } \
	&& { printf '%s\n' "$$logs" | grep -q '"event": "http_request"' \
		|| { echo "smoke: o access log estruturado (http_request) nao saiu" >&2; false; }; } \
	&& { ! printf '%s\n' "$$logs" | grep -q 'uvicorn.access' \
		|| { echo "smoke: o uvicorn escreveu access log (--no-access-log nao vale)" >&2; false; }; } \
	&& { ! printf '%s\n' "$$logs" | grep -q 'automatic telemetry' \
		|| { echo "smoke: o FastAPI avisou que a telemetria automatica falhou (telemetry auto_configure)" >&2; false; }; } \
	&& { $(SMOKE_COMPOSE) exec -T -e ENVIRONMENT=production api python -c "$(SMOKE_GUARDA)" 2>&1 \
		| grep -q 'ORCAMENTO_LINK_SECRET usa o valor de demonstracao' \
		|| { echo "smoke: com ENVIRONMENT=production a imagem aceita o segredo de demonstracao" >&2; false; }; } \
	&& echo "smoke ok: readiness 200, rota autenticada sem token 401, sem header server, usuario 1001, boot em JSON, sem access log do uvicorn, sem aviso de telemetria, metricas dos quatro processos, segredo de demonstracao recusado em producao" \
	|| status=$$?; \
	if [ $$status -ne 0 ]; then $(SMOKE_COMPOSE) logs --no-color --tail=200; fi; \
	$(SMOKE_COMPOSE) down -v; \
	exit $$status

# Manifests do Kubernetes (k8s/), os tres overlays: kubeconform com os schemas
# do Kubernetes 1.35, a versao do no do kind (Secret reprova: as senhas vem do
# make deploy do platform), e trivy config sem achado HIGH ou CRITICAL (o que
# ele ignora, com o motivo, esta em k8s/trivy-ignore.rego; a excecao vale para o
# ConfigMap do replica-set.js inteiro, e confere-senhas-do-script.sh exige que
# todo campo de senha do script venha de segredo(...)), nas versoes do
# platform. Tambem reprova se a MONGODB_URI de algum processo ficar com o valor
# sentinela, o que e o replacements do base nao ter chegado nela. O job build
# do CI roda este alvo.
KUBERNETES_VERSION := 1.35.0
KUBECONFORM_IMAGE := ghcr.io/yannh/kubeconform:v0.8.0
# Fonte unica da versao do trivy: o security.yml instala a de print-trivy-version.
TRIVY_VERSION := 0.72.0
TRIVY_IMAGE := aquasec/trivy:$(TRIVY_VERSION)

manifests:
	@mkdir -p reports
	@scripts/ci/confere-senhas-do-script.sh
	@for overlay in kind kind-ci k3s; do \
		echo ">> k8s/overlays/$$overlay: kubeconform e trivy config"; \
		kubectl kustomize k8s/overlays/$$overlay > reports/k8s-$$overlay.yaml || exit 1; \
		! grep -q 'copiado de api.yaml' reports/k8s-$$overlay.yaml \
			|| { echo "MONGODB_URI sem o valor de api.yaml: confira o replacements de k8s/base/kustomization.yaml" >&2; exit 1; }; \
		docker run --rm -i $(KUBECONFORM_IMAGE) -strict -summary -output text \
			-kubernetes-version $(KUBERNETES_VERSION) -reject Secret - \
			< reports/k8s-$$overlay.yaml || exit 1; \
		docker run --rm -i -v "$(CURDIR)/k8s/trivy-ignore.rego:/trivy-ignore.rego:ro" \
			--entrypoint sh $(TRIVY_IMAGE) -c 'cat > /tmp/manifests.yaml && trivy config \
			--quiet --severity HIGH,CRITICAL --exit-code 1 \
			--ignore-policy /trivy-ignore.rego /tmp/manifests.yaml' \
			< reports/k8s-$$overlay.yaml || exit 1; \
	done

print-trivy-version:
	@echo $(TRIVY_VERSION)

# Implanta o servico no kind da plataforma pelo script do CD do platform:
# constroi a imagem com o commit como tag, carrega no kind e aplica o overlay
# na ordem do contrato (Job de inicializacao, banco e rollouts). Antes, a
# plataforma no ar: make -C $(PLATFORM_DIR) kind-up deploy (README, Implantacao).
# PLATFORM_DIR (o clone do platform) e KIND_OVERLAY (kind, kind-ci ou k3s) sao
# os nomes do Execution Service. OVERLAY e o nome antigo do segundo e segue
# valendo.
PLATFORM_DIR ?= ../postech-sw-arch-p4-platform
KIND_OVERLAY ?= $(if $(OVERLAY),$(OVERLAY),kind)

kind-deploy:
	$(PLATFORM_DIR)/scripts/ci/implantar-servicos.sh --overlay $(KIND_OVERLAY) \
		billing-service=$(CURDIR)

# API local apontando para o MongoDB do compose (make compose-up). Sem o access
# log do uvicorn, como na imagem: o http_request estruturado sai do middleware.
run:
	ENVIRONMENT=development $(PY) uvicorn src.main:criar_app --factory --reload --port 8002 --no-access-log

compose-up:
	$(COMPOSE) up -d --build --wait

compose-down:
	$(COMPOSE) down -v

compose-logs:
	$(COMPOSE) logs -f api prazos relay consumidor

seed:
	$(COMPOSE) exec api python -m src.seed
