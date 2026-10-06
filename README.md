# PytStop fase 4: Billing Service

Serviço de orçamento e pagamento: tabela de preços de serviços e peças, geração de orçamento, decisão do cliente e pagamento via Mercado Pago, com estorno na compensação da saga. Banco próprio: MongoDB.

Parte da fase 4 do Tech Challenge (FIAP Pós Tech, Software Architecture, 15SOAT): o PytStop, sistema de gestão de oficina mecânica das fases anteriores, refatorado em microsserviços com Saga Pattern, mensageria assíncrona, CI/CD por serviço e deploy automatizado em Kubernetes.

**Status:** domínio, casos de uso, API e persistência prontos. Os eventos de integração já são gravados no outbox (coleção `outbox`, na mesma transação do estado); o relay para o RabbitMQ e o consumidor de comandos entram no próximo PR.

## Repositórios da fase 4

| Repositório | Papel |
|---|---|
| [postech-sw-arch-p4-os-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service) | Ordens de serviço, clientes e veículos, usuários internos e orquestrador da saga |
| [postech-sw-arch-p4-billing-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service) | Orçamentos, pagamentos via Mercado Pago e tabela de preços |
| [postech-sw-arch-p4-execution-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service) | Fila de diagnóstico e execução e estoque de peças |
| [postech-sw-arch-p4-platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform) | Infraestrutura compartilhada, testes E2E, arquitetura global e entrega |

A `main` é protegida desde o primeiro commit: toda mudança entra por pull request com squash.

## Arquitetura do serviço

DDD em camadas (`dominio` ← `aplicacao` ← `infraestrutura`/`interfaces`), com três contextos e a base técnica copiada do p3 (`compartilhado`). O import-linter trava as camadas e a independência entre contextos: orçamento lê preços e pagamento lê orçamento por portas definidas no consumidor, com adapters na infraestrutura.

| Contexto | Agregados | Regras principais |
|---|---|---|
| `precos` | `PrecoServico` (código), `PrecoPeca` (SKU) | CRUD do admin; validação de itens devolve os códigos inexistentes ou inativos |
| `orcamento` | `Orcamento` | um por ordem de serviço; preço congelado na geração; `PENDENTE → APROVADO \| RECUSADO \| EXPIRADO`, `PENDENTE \| APROVADO → CANCELADO`; decisão pelo link público (token HMAC com expiração) ou pelo atendente |
| `pagamento` | `Pagamento` | um por orçamento; `PENDENTE → APROVADO \| RECUSADO \| EXPIRADO \| ESTORNADO` (compensação antes do pagamento), `APROVADO → ESTORNADO`; status sempre confirmado na consulta ao provedor |

Transações: MongoDB 7 em replica set de um nó. Cada caso de uso roda numa transação (`snapshot` + `majority`) que grava o agregado e o evento no outbox juntos; conflito de escrita (`WriteConflict`) faz o trabalho ser repetido do zero, relendo o estado. Assim, decisão do cliente e expiração concorrentes nunca gravam os dois resultados. Dinheiro é `Decimal` no domínio, `Decimal128` no banco e string decimal nas mensagens.

Mercado Pago: porta `GatewayPagamento` com dois adapters. `MercadoPagoGateway` (Checkout Pro via httpx: timeout, retry com backoff só em operação idempotente, circuit breaker) e `GatewayPagamentoSimulado`, o padrão em dev, CI e demo, controlado pelo checkout simulado. O webhook valida `x-signature` (HMAC-SHA256 do manifesto `id:<data.id>;request-id:<x-request-id>;ts:<ts>;`) e nunca usa o status do corpo: sempre consulta `GET /v1/payments/{id}`.

Regras de dinheiro do pagamento:

- No Checkout Pro o comprador pode tentar de novo depois de um cartão recusado, então tentativa recusada não encerra a cobrança: ela só fecha sem pagamento quando expira (a preferência expira junto, inclusive Pix e boleto). O simulador tem o botão Recusar para demonstrar a recusa definitiva.
- Aprovação com valor diferente do orçado, ou que chega com a cobrança já encerrada (expirada, recusada, estornada ou paga por outra transação), é estornada na hora, com chave de idempotência `estorno-automatico-<id do pagamento no provedor>`.
- Na compensação da saga, `EstornarPagamento` estorna no provedor o pagamento aprovado e encerra a cobrança ainda pendente (nada a devolver); os dois respondem `PagamentoEstornado`. Estorno ainda em processamento no provedor é repetido com a mesma chave.

## Como rodar

Requisitos: Python 3.14 com [uv](https://docs.astral.sh/uv/) e Docker.

```bash
make compose-up      # API (porta 8002), processo de prazos e MongoDB em replica set; roda o seed de preços
curl localhost:8002/api/v1/saude
make compose-down
```

Swagger em `http://localhost:8002/docs`. As rotas internas validam o JWT emitido pelo OS Service (`JWKS_URL`); sem ele no ar elas respondem 503, enquanto o link público do cliente, o webhook e o simulador continuam funcionando.

| Variável | Padrão (dev) | Para que |
|---|---|---|
| `ENVIRONMENT` | `production` se ausente | `development`/`test` liberam os padrões abaixo; o compose usa `development` |
| `MONGODB_URI`, `MONGODB_DB` | `mongodb://localhost:27017/?directConnection=true`, `billing` | banco (replica set) |
| `JWKS_URL`, `JWT_ISSUER`, `JWT_AUDIENCE` | `http://localhost:8001/.well-known/jwks.json`, `pytstop-os-service`, `pytstop` | validação do JWT RS256 |
| `BILLING_PUBLIC_URL` | `http://localhost:8002` | base do link de decisão e do checkout simulado |
| `ORCAMENTO_LINK_SECRET` | valor de demonstração | segredo HMAC do link (fora de dev/test: obrigatório, ≥ 32 bytes, nunca o de demonstração) |
| `ORCAMENTO_VALIDADE_HORAS`, `PAGAMENTO_VALIDADE_MINUTOS` | `72`, `60` | prazos da espera humana (aceitam fração, para demo) |
| `MP_MODE` | `simulado` | `mercadopago` exige `MP_ACCESS_TOKEN` e `MP_WEBHOOK_SECRET` |
| `MP_API_URL`, `MP_NOTIFICATION_URL`, `MP_TIMEOUT_SEGUNDOS` | API pública, `<BILLING_PUBLIC_URL>/api/v1/webhooks/mercadopago`, `5` | adapter real |
| `PRAZOS_INTERVALO_SEGUNDOS` | `30` | ciclo do processo `prazos` |

Processos da imagem (`entrypoint.sh`): `api` (padrão; com `RUN_SEED_ON_STARTUP=true` roda o seed idempotente antes) e `prazos` (expira orçamentos e pagamentos vencidos).

## API

| Rota | Quem | O que faz |
|---|---|---|
| `POST/GET /api/v1/precos/servicos`, `GET/PUT/DELETE /api/v1/precos/servicos/{codigo}` | escrita: admin; leitura: usuário interno | preços de serviços (`DELETE` desativa) |
| `POST/GET /api/v1/precos/pecas`, `GET/PUT/DELETE /api/v1/precos/pecas/{sku}` | idem | preços de peças |
| `POST /api/v1/precos/validacao` | mecânico | `{servicos[], pecas[]}` → `{invalidos[]}` (chamada síncrona da Execução) |
| `GET /api/v1/orcamentos/{id}`, `GET /api/v1/orcamentos?ordem_id=` | atendente | consulta |
| `POST /api/v1/orcamentos/{id}/decisao` | atendente | `{"decisao": "aprovar" \| "recusar"}` em nome do cliente |
| `GET /api/v1/publico/orcamentos/{token}`, `POST .../{token}/decisao` | cliente (link) | consulta e decisão sem login |
| `GET /api/v1/pagamentos/{id}` | atendente | consulta, com o histórico de notificações |
| `POST /api/v1/webhooks/mercadopago` | Mercado Pago | notificação assinada |
| `GET /simulador/checkout/{pagamento_id}`, `POST /api/v1/simulador/pagamentos/{id}/aprovar\|recusar` | cliente (só `MP_MODE=simulado`) | checkout simulado |
| `GET /api/v1/saude`, `GET /metrics` | todos | liveness/readiness e métricas Prometheus |

Admin passa em todas as rotas internas. Todo erro sai no envelope `{"erro": {"codigo", "mensagem", "id_requisicao"}}`; o 422 de validação acrescenta `detalhes` (campo e regra, sem ecoar o valor recebido). `PUT` de preço é substituição completa (`ativo` obrigatório).

```bash
TOKEN=...   # JWT de admin emitido pelo OS Service
curl -X POST localhost:8002/api/v1/precos/validacao -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"servicos": ["SRV-FREIOS"], "pecas": ["PEC-VELA", "PEC-X"]}'
# {"invalidos": ["PEC-X"]}
```

## Eventos gravados no outbox

Envelope da RFC-004 (`id` UUIDv7, `tipo`, `versao`, `origem`, `correlation_id` = ordem de serviço, `causation_id`, `ocorrido_em`, `dados`): `OrcamentoGerado` (com `link_decisao`), `GeracaoDeOrcamentoFalhou`, `OrcamentoAprovado`, `OrcamentoRecusado`, `OrcamentoExpirado`, `OrcamentoCancelado`, `PagamentoSolicitado` (com `checkout_url`), `PagamentoConfirmado`, `PagamentoRecusado`, `PagamentoExpirado`, `PagamentoEstornado`, `EstornoDePagamentoFalhou`. Repetir `GerarOrcamento`, `SolicitarPagamento`, `CancelarOrcamento` ou `EstornarPagamento` já concluído não duplica documento nem evento; um comando que falha (`GeracaoDeOrcamentoFalhou`, `EstornoDePagamentoFalhou`) responde de novo a cada repetição.

## Testes

```bash
make check   # ruff (lint + format), import-linter, mypy strict, bandit e pytest com gate de cobertura de 90%
```

Unitários (domínio, link assinado, contrato do adapter do Mercado Pago com respx, circuit breaker, JWT com chave RSA gerada no teste) e integração com MongoDB real em replica set via testcontainers (casos de uso, API, idempotência, corrida entre decisão e expiração). O gate exige 90% de cobertura de linhas e ramos (`.coveragerc`); o CI publica o resumo por pacote no summary do job `test`.
