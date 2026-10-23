# PytStop fase 4: Billing Service

[![CD](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service/actions/workflows/cd.yml/badge.svg?branch=main)](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service/actions/workflows/cd.yml)

Serviço de orçamento e pagamento do PytStop, o sistema de gestão da oficina mecânica. Mantém a tabela de preços de serviços e peças e gera o orçamento com preços congelados. Registra a decisão do cliente (pelo link público ou pelo atendente) e cobra no Mercado Pago, com estorno na compensação da saga. É participante da saga que o OS Service orquestra: recebe os comandos `GerarOrcamento`, `CancelarOrcamento`, `SolicitarPagamento` e `EstornarPagamento` pela fila `billing.comandos` e responde com eventos, gravados na outbox e publicados pelo relay. Banco próprio: MongoDB 7 em replica set. Integrações: RabbitMQ, Mercado Pago (Checkout Pro), o JWKS do OS Service (para validar o JWT) e a chamada síncrona da Execução para validar preços. Faz parte da entrega da fase 4 do Tech Challenge, e o [índice da entrega](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/INDICE-DA-ENTREGA.md) liga os quatro repositórios, os requisitos e as evidências.

**Sumário**: [Como rodar](#como-rodar), [Como testar](#como-testar), [Cobertura](#cobertura), [API](#api), [Mensageria](#mensageria), [Arquitetura](#arquitetura), [Observabilidade](#observabilidade), [CI/CD](#cicd), [Segurança](#segurança) e [Decisões e rastreabilidade](#decisões-e-rastreabilidade).

**Documentos em `docs/`**: [arquitetura](docs/arquitetura.md), [regras do serviço](docs/regras-do-servico.md), [API](docs/api/) e [exemplos da API](docs/api/exemplos.md), [mensageria](docs/mensageria.md), [observabilidade](docs/observabilidade.md), [como rodar](docs/como-rodar.md), [implantação](docs/implantacao.md), [testes](docs/testes.md), [cobertura](docs/cobertura.md), [mutação](docs/mutacao.md), [CD](docs/cicd.md), [dependências](docs/dependencias.md) e [troca de senhas e chaves](docs/troca-de-senhas-e-chaves.md).

## Como rodar

Requisitos: Python 3.14 com [uv](https://docs.astral.sh/uv/) e Docker.

```bash
make compose-up
curl localhost:8002/api/v1/saude
curl localhost:8002/api/v1/saude/pronto
make compose-down
```

O `make compose-up` sobe o init do banco, a API (porta 8002), o `prazos`, o relay, o consumidor, o MongoDB e o RabbitMQ, com o pagamento simulado, e cadastra os preços de demonstração. O Swagger fica em `http://localhost:8002/docs`. As variáveis de ambiente, os processos da imagem e o `make smoke` estão em [docs/como-rodar.md](docs/como-rodar.md).

No kind, com o repositório [platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform) clonado ao lado deste:

```bash
make -C ../postech-sw-arch-p4-platform kind-up deploy
make kind-deploy
curl -k https://localhost/billing/api/v1/saude
```

O `make kind-deploy` constrói a imagem do commit atual, carrega no kind e aplica o overlay `kind`. Manifests, sondas, rede e segredos estão em [docs/implantacao.md](docs/implantacao.md), e o passo a passo do kind, em [docs/como-rodar.md](docs/como-rodar.md#no-kind-local).

## Como testar

```bash
make check
make smoke
```

O `make check` pede Docker. Ele confere o `uv.lock`, roda o ruff, o `import-linter`, o actionlint, o mypy em modo strict e o bandit e executa o pytest com o gate de cobertura de 95% e o `diff-cover` de 90% no código novo. O `make smoke` prova a imagem pelo entrypoint real, como o smoke do CI.

- **Unitários**: domínio e estados dos agregados, adapter do Mercado Pago (respx), circuit breaker, JWKS, contrato das mensagens, configuração e telemetria.
- **Integração**: testcontainers com MongoDB 7.0.43 em replica set e RabbitMQ 4.3.6 reais, para a API, os casos de uso, o rastreamento, o relay e o consumidor.
- **Contrato** (`tests/contratos`): a cópia de `contratos/` confere com o `platform` no commit de origem.
- **Documentos**: o OpenAPI, a coleção Postman, os `curl` dos exemplos, a arquitetura e os links seguem o código.
- **BDD**: o fluxo completo da saga em Gherkin é do `platform` ([`saga_atendimento.feature`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/e2e/features/saga_atendimento.feature)) e roda no `deploy-kind` do CD deste serviço.

Cada tipo de teste, o gate e os resumos do CI estão em [docs/testes.md](docs/testes.md).

### Mutação

Mutantes escritos à mão nos módulos críticos (o `mutmut` não roda neste repositório): 74 de 74 medidos foram mortos em 23/10/2026, com 2 equivalentes de fora da conta, sem gate de CI. A lista, a tabela por área e como refazer estão em [docs/mutacao.md](docs/mutacao.md).

## Cobertura

O gate é de 95% no total, com linhas e ramos somados (`fail_under` no `.coveragerc`), e o código novo precisa de 90% (`make diff-cover`), como pede o ADR-041 (RNF-050). As tabelas não são digitadas: o [`scripts/cobertura_resumo.py`](scripts/cobertura_resumo.py) as escreve a partir do `coverage.xml` do `make test`, com o link do run do CI que tem o mesmo `src/`.

<!-- cobertura:inicio -->
**100,00% no gate** (linhas e ramos somados): 4.551 linhas, 4.551 cobertas (100,0%), e 624 ramos, 624 cobertos (100,0%), medidos pelo `make test` com o `src/` do [run 39935825688 do CI](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service/actions/runs/39935825688). Tabelas geradas por `scripts/cobertura_resumo.py` a partir do `coverage.xml` desse `make test`.

| Contexto | Linhas | Cobertas | Cobertura |
|---|---:|---:|---:|
| `src` (processos) | 570 | 570 | 100,0% |
| `src/compartilhado` | 1.580 | 1.580 | 100,0% |
| `src/orcamento` | 672 | 672 | 100,0% |
| `src/pagamento` | 1.292 | 1.292 | 100,0% |
| `src/precos` | 437 | 437 | 100,0% |
| **total** | 4.551 | 4.551 | 100,0% |

| Camada | Linhas | Cobertas | Cobertura |
|---|---:|---:|---:|
| `dominio` | 895 | 895 | 100,0% |
| `aplicacao` | 773 | 773 | 100,0% |
| `infraestrutura` | 1.665 | 1.665 | 100,0% |
| `interfaces` | 648 | 648 | 100,0% |

<!-- cobertura:fim -->

Para refazer depois de mudar `src/`: `make cobertura-readme RUN=<id do run do CI>`. Oscilação: com o mesmo `src/`, o `make test` no macOS e o CI medem o mesmo valor, o da tabela ([como ler uma diferença](docs/cobertura.md#oscilação)). O gate, o `diff-cover` e o que fica fora da medição estão em [docs/cobertura.md](docs/cobertura.md).

## API

O Swagger fica em `/docs` (ReDoc em `/redoc`), em todo ambiente; no kind, em `https://localhost/billing/docs`. O OpenAPI exportado ([`docs/api/openapi.json`](docs/api/openapi.json)) e a coleção Postman v2.1 ([`docs/api/billing.postman_collection.json`](docs/api/billing.postman_collection.json)) saem do próprio app pelo `make docs-api` (RNF-052). Um `curl` por rota está em [docs/api/exemplos.md](docs/api/exemplos.md). A coleção do fluxo completo, que roda com newman, é do `platform`.

| Rota (prefixo `/api/v1`) | Papéis | O que faz |
|---|---|---|
| `/precos/servicos` e `/precos/pecas` | leitura: atendente, mecânico, admin; escrita: admin | tabela de preços |
| `POST /precos/validacao` | mecânico, admin | valida os itens (chamada síncrona da Execução) |
| `/orcamentos` | atendente, admin | consulta e decisão em nome do cliente |
| `/publico/orcamentos/{token}` | cliente (link) | consulta e decisão sem login |
| `GET /pagamentos/{id}` | atendente, admin | consulta, com as tentativas vistas no provedor |
| `POST /webhooks/mercadopago` | Mercado Pago | notificação assinada |
| `/saude` e `/saude/pronto` | — | liveness e readiness |

Os erros usam o envelope `{"erro": {"codigo", "mensagem", "id_requisicao"}}`: 401 `NAO_AUTENTICADO`, 403 `ACESSO_NEGADO`, 404 `ENTIDADE_NAO_ENCONTRADA`, 409 para regra de negócio, 422 `VALOR_INVALIDO` e 503 para dependência fora. O 422 de validação de schema é a exceção: segue o formato do FastAPI, sem ecoar o valor recebido. A tabela completa de rotas, com o simulador do checkout e o `/metrics`, e os detalhes dos erros estão em [docs/api/](docs/api/README.md).

## Mensageria

RabbitMQ 4.3.6 com a topologia da plataforma (ADR-036; RFC-004, seções 5.1 a 5.5): o serviço só faz declaração passiva, e cada processo confere apenas o que o usuário `billing` alcança.

| | Fila ou exchange | Mensagens |
|---|---|---|
| Consome | `billing.comandos` (ligada a `comando.billing.#` no `pytstop.comandos`) | `GerarOrcamento`, `CancelarOrcamento`, `SolicitarPagamento` e `EstornarPagamento`, publicados pelo usuário `os` |
| Publica | `pytstop.eventos`, routing key `evento.billing.<tipo em snake_case>` | os 13 eventos da seção [Eventos](docs/mensageria.md#eventos), com o usuário `billing` |
| Retry | `pytstop.retry`, routing key `billing.comandos.retry.<1s\|5s\|15s\|60s\|300s>` | cópia do comando com `x-tentativa` de 1 a 5 |

- **Outbox e relay**: o caso de uso grava o evento na outbox, na mesma transação do estado, e o relay (`python -m src.relay`) o publica com confirmação do broker. Cada linha é reivindicada com lease e token, então mais de um relay roda ao mesmo tempo, e a quinta falha de publicação a marca `dead`.
- **Consumidor** (`python -m src.consumidor`): prefetch 1, ack manual e handler na transação da própria mensagem. O mesmo `id` de novo republica o desfecho registrado, sem repetir o efeito (idempotência). Erro transitório volta pela fila de retry; erro permanente, a sexta falha ou a cópia recusada vão para `billing.comandos.dlq`, e o redrive é `make redrive` do `platform`.
- **Contratos**: [`contratos/`](contratos) é cópia do `platform` no commit de `contratos/ORIGEM`: os schemas e exemplos das mensagens, o `asyncapi.yaml` e a topologia do broker.

A participação na saga (comando, evento de resposta e repetição), o envelope, a saúde dos processos, a retenção, o reprocesso de linha `dead` e as métricas estão em [docs/mensageria.md](docs/mensageria.md); os 13 eventos, em [Eventos](docs/mensageria.md#eventos).

## Arquitetura

DDD + arquitetura em camadas (`dominio` ← `aplicacao` ← `infraestrutura` ← `interfaces`), com contratos verificados pelo `import-linter`: camadas por contexto, domínio e aplicação sem framework nem driver, `compartilhado` sem conhecer os contextos e núcleos independentes (orçamento lê preços e pagamento lê orçamento por portas definidas no consumidor, com adapters na infraestrutura).

| Contexto | Agregados | Responsabilidade |
|---|---|---|
| `precos` | `PrecoServico` (código), `PrecoPeca` (SKU) | CRUD do admin; validação de itens devolve os códigos inexistentes ou inativos |
| `orcamento` | `Orcamento` (um por ordem) | Preço congelado nas linhas na geração; `PENDENTE → APROVADO \| RECUSADO \| EXPIRADO \| CANCELADO` e `APROVADO → CANCELADO`; decisão única, pelo link público ou pelo atendente |
| `pagamento` | `Pagamento` (um por ordem e por orçamento) | Cobrança no provedor; `SOLICITADO → CONFIRMADO \| RECUSADO \| EXPIRADO \| CANCELADO`, e qualquer um deles `→ ESTORNADO`; status sempre confirmado na consulta ao provedor |

As camadas e os módulos, o modelo das coleções do MongoDB e os fluxos com diagramas de sequência estão em [docs/arquitetura.md](docs/arquitetura.md) (RNF-051). As regras de implementação (transações e outbox, dinheiro, banco, Mercado Pago e o processo `prazos`) estão em [docs/regras-do-servico.md](docs/regras-do-servico.md). A arquitetura global da fase 4 está na [RFC-004](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/rfc/fase4/rfc-004-microsservicos-saga.md) e nos [ADRs 034 a 043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/tree/main/docs/arquitetura) do `platform` (mais em [Decisões e rastreabilidade](#decisões-e-rastreabilidade)).

## Observabilidade

Traces e métricas seguem o [ADR-043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/043-observabilidade-distribuida.md). Os quatro processos (API, relay, consumidor e `prazos`) exportam os traces por OTLP/gRPC para o Jaeger quando `OTEL_ENABLED=true`. As métricas são as do Prometheus, no `/metrics` de cada processo (a API na porta 8000 e os outros três na 9100). Os logs são uma linha JSON por evento no stdout, com `correlation_id`, `trace_id` e `span_id` e com os dados pessoais mascarados; no cluster, o Promtail do `platform` os leva ao Loki.

Os painéis do Grafana (Orçamentos, Pagamentos, Saga, Mensageria, Serviços e Bancos), os alertas e o [catálogo das métricas dos três serviços](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/README.md#catálogo-de-métricas) ficam no `platform`. Com o kind no ar, o comando abaixo abre o Jaeger em `localhost:16686`, o Grafana em `localhost:3000` e o Prometheus em `localhost:9090`:

```bash
make -C ../postech-sw-arch-p4-platform port-forward
```

Os spans, a amostragem, [o que não é exportado, e por quê](docs/observabilidade.md#o-que-não-é-exportado-e-por-quê), a tabela de métricas, os logs e como filtrar no Jaeger estão em [docs/observabilidade.md](docs/observabilidade.md).

## CI/CD

A `main` é protegida desde o primeiro commit: toda mudança entra por pull request com squash e os 9 checks do CI verdes.

Os workflows ficam em [`.github/workflows`](.github/workflows): o `ci.yml` (lint, tipos, segurança do código, testes com gate de cobertura, SonarQube e build da imagem com manifests e smoke), o `security.yml` (pip-audit, gitleaks e trivy) e o [`cd.yml`](.github/workflows/cd.yml). Os dois primeiros somam os 9 checks. O `cd.yml` roda na `main` depois do CI e da segurança verdes e chama o workflow reutilizável do `platform`, fixado por SHA: ele publica a imagem no GHCR e implanta num kind efêmero (Kubernetes dentro de contêineres do runner), com smoke e E2E da saga. O Dependabot atualiza o uv, as actions e as imagens do Docker toda semana.

O [CD automatizado](docs/cicd.md), as [dependências automatizadas](docs/dependencias.md) e a [implantação no Kubernetes](docs/implantacao.md) estão detalhados em `docs/`.

## Segurança

- **Autenticação (ADR-039)**: JWT RS256 emitido pelo OS Service, validado pela chave pública do JWKS (`JWKS_URL`, timeout de 2 s, cópia fresca por 10 min; com o OS fora, a última cópia boa vale por até 1 h e 3 falhas seguidas abrem um circuit breaker por 30 s), conferindo assinatura, `iss` (`pytstop-os-service`), `aud` (`pytstop`), `exp`, `sub` (UUID do usuário), `type=access` e `papel`, com 10 s de tolerância de relógio. Toda falha de credencial responde o mesmo 401; papel válido sem permissão, 403; JWKS indisponível e sem cópia, 503 com `Retry-After`.
- **Link de decisão**: token HMAC do orçamento e do prazo (segundo cheio, igual ao `valido_ate`), sem login. Token inválido ou expirado, orçamento inexistente ou já decidido: o mesmo 404, na consulta e na decisão.
- **Webhook e provedor**: o webhook valida a `x-signature` (HMAC-SHA256) e consulta o provedor para saber o status, sem confiar no corpo. O texto livre da recusa do Mercado Pago não entra em log, e o evento e a API levam uma mensagem fixa ([regras do serviço](docs/regras-do-servico.md#mercado-pago)).
- **Papéis e LGPD**: a tabela de papéis por rota e o parágrafo da LGPD estão em [docs/arquitetura.md](docs/arquitetura.md#6-segurança-e-lgpd). O Billing não guarda nome, documento nem contato de cliente e, por isso, não tem fluxo de anonimização.
- **Segredos e rede**: nenhum segredo está nos manifests, e o `make deploy` do `platform` os gera. Os pods rodam sem root, com a raiz somente leitura, e a NetworkPolicy nega a entrada por padrão ([docs/implantacao.md](docs/implantacao.md)). O CI roda `pip-audit`, `trivy` e `gitleaks`.

### Troca de senhas e chaves

As senhas do MongoDB (`billing`, `exporter` e `root`), o keyfile e a chave do link de decisão (`ORCAMENTO_LINK_SECRET`) trocam por procedimentos de `kubectl`, um por credencial, em [docs/troca-de-senhas-e-chaves.md](docs/troca-de-senhas-e-chaves.md). A mecânica geral é a de [Troca de senha do banco](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform#troca-de-senha-do-banco), no README do `platform`.

## Decisões e rastreabilidade

Este repositório é um dos quatro da entrega da fase 4 do Tech Challenge (FIAP Pós Tech, Software Architecture, 15SOAT). Nessa fase o PytStop, sistema de gestão de oficina mecânica das fases anteriores, virou microsserviços com Saga Pattern, mensageria assíncrona, CI/CD por serviço e deploy automatizado em Kubernetes. O [índice da entrega](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/INDICE-DA-ENTREGA.md) liga os quatro repositórios, os requisitos e as evidências.

A arquitetura global da fase 4 está na [RFC-004 e nos ADRs 034 a 043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/tree/main/docs/arquitetura) do repositório `platform`: divisão dos serviços, saga, catálogo de mensagens, rotas, dados e segurança. Este serviço segue em especial o ADR-036 (mensageria), o ADR-037 (banco por serviço) e o ADR-039 (autenticação entre serviços). Também seguem o ADR-040 (Mercado Pago), o ADR-041 (testes e qualidade), o ADR-042 (CI/CD) e o ADR-043 (observabilidade).

As decisões deste serviço que não estão nos ADRs ficam na [seção 8 de docs/arquitetura.md](docs/arquitetura.md#8-decisões). A [matriz de rastreabilidade](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/entrega/matriz-de-rastreabilidade.md) do `platform` liga cada requisito aos pull requests e às evidências.

Parte do código vem do PytStop da fase 3: o `src/compartilhado` é um recorte do [`postech-sw-arch-p3` no commit `08dcffe`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/08dcffe6365ece594f438cdbc4c5eef1d88ebfb1), e o `catalogo_servicos` virou a tabela de preços. O que veio, o que foi reescrito para o MongoDB e o RabbitMQ e o que ficou de fora estão em [docs/regras-do-servico.md](docs/regras-do-servico.md#proveniência).

### Repositórios da fase 4

| Repositório | Papel |
|---|---|
| [postech-sw-arch-p4-os-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service) | Ordens de serviço, clientes e veículos, usuários internos e orquestrador da saga |
| [postech-sw-arch-p4-billing-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service) | Orçamentos, pagamentos via Mercado Pago e tabela de preços |
| [postech-sw-arch-p4-execution-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service) | Fila de diagnóstico e execução e estoque de peças |
| [postech-sw-arch-p4-platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform) | Infraestrutura compartilhada, testes E2E, arquitetura global e entrega |
