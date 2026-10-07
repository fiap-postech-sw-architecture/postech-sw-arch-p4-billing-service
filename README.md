# PytStop fase 4: Billing Service

Serviço de orçamento e pagamento da oficina: tabela de preços de serviços e peças, geração do orçamento com preços congelados, decisão do cliente (link público ou atendente) e cobrança no Mercado Pago, com estorno na compensação da saga. Banco próprio: MongoDB 7 em replica set.

Parte da fase 4 do Tech Challenge (FIAP Pós Tech, Software Architecture, 15SOAT): o PytStop, sistema de gestão de oficina mecânica das fases anteriores, refatorado em microsserviços com Saga Pattern, mensageria assíncrona, CI/CD por serviço e deploy automatizado em Kubernetes.

O serviço participa da saga de atendimento orquestrada pelo OS Service: o processo `consumidor` recebe pela fila `billing.comandos` do RabbitMQ os comandos da saga (`GerarOrcamento`, `CancelarOrcamento`, `SolicitarPagamento`, `EstornarPagamento`), cada caso de uso grava a resposta na outbox transacional (coleção `outbox`) na mesma transação do estado, e o processo `relay` a publica no exchange `pytstop.eventos` (seção [Mensageria](#mensageria)).

Arquitetura da fase 4: [RFC-004 e ADRs 034 a 043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/tree/main/docs/arquitetura) no repositório `platform` (divisão dos serviços, saga, catálogo de mensagens, rotas, dados e segurança). Este serviço segue em especial o ADR-036 (mensageria), o ADR-037 (banco por serviço), o ADR-039 (autenticação entre serviços), o ADR-040 (Mercado Pago), o ADR-041 (testes e qualidade), o ADR-042 (CI/CD) e o ADR-043 (observabilidade).

## Repositórios da fase 4

| Repositório | Papel |
|---|---|
| [postech-sw-arch-p4-os-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service) | Ordens de serviço, clientes e veículos, usuários internos e orquestrador da saga |
| [postech-sw-arch-p4-billing-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service) | Orçamentos, pagamentos via Mercado Pago e tabela de preços |
| [postech-sw-arch-p4-execution-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service) | Fila de diagnóstico e execução e estoque de peças |
| [postech-sw-arch-p4-platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform) | Infraestrutura compartilhada, testes E2E, arquitetura global e entrega |

A `main` é protegida desde o primeiro commit: toda mudança entra por pull request com squash e os 9 checks do CI verdes.

## Arquitetura

DDD + arquitetura em camadas (`dominio` ← `aplicacao` ← `infraestrutura` ← `interfaces`), com contratos verificados pelo `import-linter`: camadas por contexto, domínio e aplicação sem framework nem driver, `compartilhado` sem conhecer os contextos e núcleos independentes (orçamento lê preços e pagamento lê orçamento por portas definidas no consumidor, com adapters na infraestrutura).

| Contexto | Agregados | Responsabilidade |
|---|---|---|
| `precos` | `PrecoServico` (código), `PrecoPeca` (SKU) | CRUD do admin; validação de itens devolve os códigos inexistentes ou inativos |
| `orcamento` | `Orcamento` (um por ordem) | Preço congelado nas linhas na geração; `PENDENTE → APROVADO \| RECUSADO \| EXPIRADO \| CANCELADO` e `APROVADO → CANCELADO`; decisão única, pelo link público ou pelo atendente |
| `pagamento` | `Pagamento` (um por ordem e por orçamento) | Cobrança no provedor; `SOLICITADO → CONFIRMADO \| RECUSADO \| EXPIRADO \| CANCELADO`, e qualquer um deles `→ ESTORNADO`; status sempre confirmado na consulta ao provedor |

- **Transações e outbox:** cada caso de uso roda numa transação do MongoDB (`snapshot` + `majority`) que grava o agregado e os eventos na outbox juntos, com o envelope validado no contrato; o `_id` da mensagem é UUIDv7, a ordem de publicação do relay. Conflito de escrita (`WriteConflict`) repete o trabalho do zero relendo o estado, então decisão e expiração concorrentes nunca gravam os dois resultados.
- **Dinheiro:** `Decimal` no domínio (VO `Dinheiro`, duas casas, até 10 dígitos inteiros, o teto do contrato das mensagens), `Decimal128` no banco e string decimal nas mensagens; nunca `float`.
- **Banco:** `python -m src.banco` (serviço `init` no compose, Job no Kubernetes) cria os índices (únicos por `ordem_id`, por orçamento e pela tentativa do provedor) e os validadores `$jsonSchema` (`moderate`) e marca a versão; API e `prazos` só conferem. Leitura tolerante a campo novo (expand/contract) e com coerência status × campos na reidratação.
- **Mercado Pago (ADR-040):** porta `GatewayPagamento` com dois adapters, escolhidos por `MP_MODE` (obrigatório, sem padrão). `MercadoPagoGateway`: Checkout Pro via httpx com timeout, retry com backoff e jitter só nas operações idempotentes, circuit breaker (5 falhas seguidas abrem por 30 s; depois, uma chamada de prova) e leitura do corpo dentro da chamada protegida (resposta fora do contrato é falha transitória). `GatewayPagamentoSimulado`: checkout próprio, só em development/test ou com `SIMULADOR_PERMITIDO=true`; o `checkout_url` leva um token assinado (HMAC, separado do link do orçamento) que as rotas do simulador exigem, com o mesmo 404 para token ausente, inválido ou expirado.
- **Webhook:** valida a `x-signature` (HMAC-SHA256 do manifesto `id:<data.id>;request-id:<x-request-id>;ts:<ts>;`) e consulta o `data.id` da query, o único que a assinatura cobre; o status vem sempre de `GET /v1/payments/{id}`, nunca do corpo. Sem `data.id` ou de outro tipo, responde 200 sem processar.
- **Tentativas e recusas:** no Checkout Pro o comprador pode tentar de novo depois de uma recusa, então cada tentativa recusada conta, e a de número `PAGAMENTO_MAX_RECUSAS` (padrão 3) encerra a cobrança como `RECUSADO`. Aprovação com valor ou moeda diferentes, ou que chega com a cobrança encerrada, é estornada na hora (chave `estorno-{pagamento_id}-{tentativa}`), registrada no agregado e publicada como `PagamentoEstornado{motivo=pagamento_apos_encerramento}`; recusa do provedor nesse estorno fica marcada no agregado, com métrica e log de erro.
- **Processo `prazos`:** a cada `PRAZOS_INTERVALO_SEGUNDOS` (30 s) concilia os pagamentos solicitados com o Mercado Pago (`GET /v1/payments/search?external_reference=`, pelo mesmo caso de uso do webhook; cobre notificação perdida) e só depois expira orçamentos e pagamentos vencidos. Encerra no fim do ciclo com SIGTERM, toca um arquivo de heartbeat a cada ciclo e expõe o instante do último ciclo em `/metrics` (porta 8000).
- **Autenticação (ADR-039):** JWT RS256 emitido pelo OS Service, validado pela chave pública do JWKS (`JWKS_URL`, timeout de 2 s, cópia fresca por 10 min; com o OS fora, a última cópia boa vale por até 1 h e 3 falhas seguidas abrem um circuit breaker por 30 s), conferindo assinatura, `iss` (`pytstop-os-service`), `aud` (`pytstop`), `exp`, `sub` (UUID do usuário), `type=access` e `papel`, com 10 s de tolerância de relógio. Toda falha de credencial responde o mesmo 401; papel válido sem permissão, 403; JWKS indisponível e sem cópia, 503 com `Retry-After`.
- **Link de decisão:** token HMAC do orçamento e do prazo (segundo cheio, igual ao `valido_ate`), sem login. Token inválido ou expirado, orçamento inexistente ou já decidido: o mesmo 404, na consulta e na decisão.

Proveniência: `src/compartilhado` parte do p3 (o PytStop da fase 3) [em `08dcffe`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/08dcffe6365ece594f438cdbc4c5eef1d88ebfb1): base de entidades e eventos, unidade de trabalho, outbox, logging JSON com mascaramento de PII, envelope de erro, middleware de cabeçalhos, métricas, padrões de teste, Dockerfile e Makefile. Do relay do p3 vieram os atrasos entre tentativas (1, 4, 16 e 64 s, `dead` na quinta), o lease, o fencing e o heartbeat em arquivo; o relay em si foi reescrito para MongoDB e RabbitMQ. Ficaram de fora o SQLAlchemy (o Billing usa MongoDB), a UI e o rate limiting da aplicação (no cluster ele fica no Kong). O `catalogo_servicos` do p3 virou a tabela de preços, com código de negócio estável e preço comercial de peça.

## Participação na saga

| Comando (OS → Billing) | Resposta (evento) | Repetição do comando |
|---|---|---|
| `GerarOrcamento` | `OrcamentoGerado{linhas, total, valido_ate, link_decisao}` ou `GeracaoDeOrcamentoFalhou{motivo, codigos_invalidos}` (diagnóstico vazio, código inexistente ou inativo, quantidade fora de 1 a 1000, total acima do teto) | republica o `OrcamentoGerado` registrado; a falha não grava nada, então a repetição reavalia e responde de novo |
| `CancelarOrcamento` | `OrcamentoCancelado` | orçamento já cancelado, recusado ou expirado responde `OrcamentoCancelado` sem mudar nada |
| `SolicitarPagamento` | `PagamentoSolicitado{valor, checkout_url, expira_em}` (só orçamento aprovado) | republica o registrado, sem nova cobrança |
| `EstornarPagamento` | cobrança aberta: fecha o checkout no provedor e responde `PagamentoCancelado` (se o provedor recusar fechar, o cancelamento conclui do mesmo jeito, com métrica e log, e a aprovação tardia é estornada); paga: estorna (`X-Idempotency-Key = estorno-{pagamento_id}`) e responde `PagamentoEstornado{motivo=compensacao}`, ou `EstornoDePagamentoFalhou` se o provedor recusar o estorno; recusada, expirada ou cancelada: `PagamentoCancelado` (nada a devolver) | republica o desfecho registrado, sem chamar o provedor |

Toda resposta leva como `causation_id` o `id` do comando respondido, inclusive o desfecho republicado para o comando repetido (pelo mesmo `id` ou por `id` novo com a mesma ordem, como no reenvio do orquestrador). `SolicitarPagamento` não tem evento de falha no contrato: orçamento ausente, de outra ordem ou não aprovado e recusa do provedor ao criar a cobrança vão para a DLQ (erro permanente, com alerta, e o orquestrador compensa pelo prazo técnico); provedor fora do ar passa pela fila de retry. As compensações acham o recurso pelo `ordem_id` (os ids são opcionais). **Lápide:** a compensação que chega antes do comando original (passo em voo, RFC-004 seção 4.5) grava o agregado já encerrado (orçamento `CANCELADO` sem linhas, pagamento `CANCELADO` sem cobrança) e responde; o original, quando chegar, encontra a lápide pelo índice único de `ordem_id` e é descartado.

Fatos que não vêm de comando: `OrcamentoAprovado`/`OrcamentoRecusado` (pelo link ou pelo atendente, com `decidido_por` = `sub` do atendente), `OrcamentoExpirado`, `PagamentoConfirmado`, `PagamentoRecusado`, `PagamentoExpirado` e o `PagamentoEstornado` do estorno automático. O `causation_id` deles é o comando que abriu o fluxo (`GerarOrcamento` para os do orçamento, `SolicitarPagamento` para os do pagamento), guardado no documento em `aberto_por` com o contexto de trace, e o evento sai como filho desse contexto: a saga segue num trace só (ADR-043).

O `PagamentoEstornado` com `motivo=pagamento_apos_encerramento` (aprovação tardia, de valor ou moeda diferentes, ou segunda tentativa paga do mesmo checkout) não responde a comando algum e pode sair com o pagamento em qualquer estado: `RECUSADO`, `EXPIRADO` e `CANCELADO` passam a `ESTORNADO`, enquanto `SOLICITADO`, `CONFIRMADO` e `ESTORNADO` ficam como estão. Quem filtra é o OS Service: a saga o ignora fora da compensação, olhando o `motivo` (ADR-040, passo 7); o Billing só garante o `motivo` no evento.

## Como rodar

Requisitos: Python 3.14 com [uv](https://docs.astral.sh/uv/) e Docker.

```bash
make compose-up      # init do banco, API (porta 8002), prazos, relay, consumidor, MongoDB e RabbitMQ; seed de precos
curl localhost:8002/api/v1/saude          # liveness: {"status": "ok", "modo": "simulado"}
curl localhost:8002/api/v1/saude/pronto   # readiness: MongoDB respondendo e preparado
make compose-logs    # logs da API, do prazos, do relay e do consumidor
make compose-down    # derruba e apaga os volumes
make smoke           # o mesmo smoke do CI, em projeto e portas proprios (inclui um comando de ponta a ponta)
```

Swagger em `http://localhost:8002/docs` (e ReDoc em `/redoc`), em todo ambiente. As rotas internas validam o JWT do OS Service (`JWKS_URL`); sem ele no ar elas respondem 503, enquanto o link público, o webhook e o simulador seguem. O rate limit das rotas públicas (link do cliente, webhook, simulador) é aplicado pelo Kong (RFC-004, seção 6); a porta do compose é só para desenvolvimento.

Variáveis (lista completa com valores de demonstração em [`.env.example`](.env.example)):

| Variável | Padrão (development/test) | Para que |
|---|---|---|
| `ENVIRONMENT` | `production` se ausente | `development`/`test` liberam os padrões abaixo; outro valor recusa o boot |
| `MONGODB_URI`, `MONGODB_DB` | `mongodb://localhost:27017/?directConnection=true`, `billing` | banco (replica set) |
| `JWKS_URL` | `http://localhost:8000/.well-known/jwks.json` | chave pública para validar o JWT (emissor e audiência são fixos, ver Autenticação) |
| `BILLING_PUBLIC_URL` | `http://localhost:8002` | base do link de decisão e do checkout simulado (https fora de development/test) |
| `ORCAMENTO_LINK_SECRET` | valor de demonstração | segredo HMAC do link e do checkout simulado (fora de development/test: obrigatório, ≥ 32 bytes, nunca o de demonstração) |
| `ORCAMENTO_VALIDADE_HORAS`, `PAGAMENTO_VALIDADE_MINUTOS` | `72`, `60` | prazo de decisão do orçamento e de pagamento da cobrança, usados por `GerarOrcamento` e `SolicitarPagamento` (casos de uso dos comandos da saga); aceitam fração, para demo |
| `PAGAMENTO_MAX_RECUSAS` | `3` | recusas do provedor que encerram a cobrança |
| `MP_MODE` | sem padrão | `simulado` ou `mercadopago` (este exige `MP_ACCESS_TOKEN` e `MP_WEBHOOK_SECRET`) |
| `SIMULADOR_PERMITIDO` | `false` | libera o simulador com `ENVIRONMENT=production` (só demonstração) |
| `MP_API_URL`, `MP_NOTIFICATION_URL`, `MP_TIMEOUT_SEGUNDOS` | API pública, `<BILLING_PUBLIC_URL>/api/v1/webhooks/mercadopago`, `5` | adapter real (https fora de development/test) |
| `PRAZOS_INTERVALO_SEGUNDOS`, `PRAZOS_HEARTBEAT`, `METRICS_PORT` | `30`, `/tmp/prazos-heartbeat`, `8000` | processo `prazos` (`METRICS_PORT` vale para os três processos sem API) |
| `RABBITMQ_URL` | sem padrão | broker com o usuário do serviço (`amqp://billing:<senha>@<host>:5672/%2F`), exigido pelo relay e pelo consumidor; o usuário vai no `user_id` de toda publicação |
| `RELAY_HEARTBEAT`, `CONSUMIDOR_HEARTBEAT` | `/tmp/relay-heartbeat`, `/tmp/consumidor-heartbeat` | arquivo de vida do relay e do consumidor |
| `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME` | `false`, `http://jaeger:4317`, `billing-service` | exportação dos traces do relay e do consumidor por OTLP/gRPC (desligada, o contexto W3C segue nas mensagens do mesmo jeito) |
| `CONTRATOS_DIR` | `contratos/` do repositório (`/app/contratos` na imagem) | schemas das mensagens |
| `RUN_SEED_ON_STARTUP` | `false` (o compose liga) | seed idempotente da tabela de preços no boot da API |

Processos da imagem (`entrypoint.sh`): `api` (padrão), `prazos`, `relay`, `consumidor` e `banco` (preparação idempotente do MongoDB, antes dos outros). A imagem roda como o usuário 1001, sem shell de login, com o sistema de arquivos só leitura no compose.

## Mensageria

RabbitMQ 4.3.6 com a topologia da plataforma (ADR-036; RFC-004, seções 5.1 a 5.5): o serviço só faz declaração passiva, e cada processo confere apenas o que o usuário `billing` alcança.

| | Fila ou exchange | Mensagens |
|---|---|---|
| Consome | `billing.comandos` (ligada a `comando.billing.#` no `pytstop.comandos`) | `GerarOrcamento`, `CancelarOrcamento`, `SolicitarPagamento` e `EstornarPagamento`, publicados pelo usuário `os` |
| Publica | `pytstop.eventos`, routing key `evento.billing.<tipo em snake_case>` | os 13 eventos da seção [Eventos](#eventos), com o usuário `billing` |
| Retry | `pytstop.retry`, routing key `billing.comandos.retry.<1s\|5s\|15s\|60s\|300s>` | cópia do comando com `x-tentativa` de 1 a 5 |

- **Envelope:** `id`, `tipo`, `versao` (1), `origem` (`billing-service`), `correlation_id` (a ordem de serviço), `causation_id`, `ocorrido_em` (UTC, do relógio injetado nos casos de uso) e `dados`; propriedades AMQP `message_id`, `correlation_id`, `type`, `user_id`, `content_type` e `delivery_mode=2`, com os headers `traceparent`/`tracestate`. O envelope é validado no JSON Schema do contrato ao gravar na outbox (fora do contrato é defeito deste serviço e aborta a transação) e ao consumir (erro permanente).
- **Outbox e relay** (`python -m src.relay`): cada documento da outbox leva o envelope, o exchange, a routing key e o `traceparent` de quem gravou. O relay reivindica uma linha por vez com `find_one_and_update` (pendente, ou em entrega com lease vencido, passa a em entrega por 30 s com um token de reivindicação novo) e publica com publisher confirms e `mandatory`; toda marcação exige o token, então um relay atrasado não marca como entregue o que outro retomou. Devolução sem rota, nack ou canal fechado pelo broker (permissão) contam tentativa, com os atrasos do p3 até `dead` na quinta; queda do broker devolve a linha sem gastar tentativa, e sem conexão o relay não reivindica nada (reconecta com backoff até 30 s). Mais de um relay pode rodar ao mesmo tempo. O relay não segura as linhas seguintes da mesma OS atrás de uma em backoff: a ordem de consumo não é garantida de qualquer forma, e quem decide é a etapa da saga (ADR-036).
- **Consumidor** (`python -m src.consumidor`): prefetch 10, ack manual. Confere o `user_id` contra o produtor do tipo (comandos são do `os`; a cópia de retry chega com o próprio `billing` e `x-tentativa` maior que zero), valida o envelope e chama o caso de uso numa unidade de trabalho que grava o `id` da mensagem em `mensagens_processadas` na transação do efeito. O mesmo `id` de novo passa pelo caso de uso, que é idempotente pela ordem e republica o desfecho registrado (conta como `duplicada`); o original atrasado depois da lápide é descartado sem resposta (`ignorada`). Erro transitório (banco, provedor ou rede fora) publica a cópia na fila de retry do nível da nova tentativa, sem `expiration` (o atraso é o TTL da própria fila, que a devolve a `billing.comandos`), espera o confirm e só então dá ack na original; a sexta falha, a cópia recusada e o erro permanente (contrato, tipo, versão, `user_id`, regra de negócio) vão para `billing.comandos.dlq` (`reject` sem requeue).
- **Retenção por índice TTL:** linhas entregues da outbox somem 7 dias depois (`entregue_em`, só com `status: entregue`) e `mensagens_processadas`, 30 dias depois; as `dead` ficam para investigação.
- **Rastreamento (ADR-043):** o relay publica num span PRODUCER filho do contexto gravado na outbox e injeta o seu no header; o consumidor abre o span CONSUMER filho da publicação, e a outbox gravada nele leva esse contexto adiante. Os logs JSON levam `correlation_id`, `trace_id` e `span_id`. Laço ocioso não abre span.
- **Métricas** (porta `METRICS_PORT` de cada processo): `pytstop_mensagens_publicadas_total{tipo}`, `pytstop_mensagens_consumidas_total{tipo,resultado}` (`processada`, `duplicada`, `ignorada`, `retry`, `dlq`) e, no relay, `outbox_pendentes` e `outbox_dead`, os nomes do p3.
- **Saúde e encerramento:** relay e consumidor tocam um arquivo a cada volta do laço (`RELAY_HEARTBEAT`, `CONSUMIDOR_HEARTBEAT`), com `pronto` só enquanto a conexão com o broker está de pé: a liveness confere a idade do arquivo, a readiness também o conteúdo. SIGTERM termina a mensagem em curso, cancela o consumo (as mensagens pré-buscadas voltam à fila) e fecha as conexões.
- **Contratos:** `contratos/` é cópia do `contratos/` do `platform` (schemas e exemplos das mensagens do Billing e o `asyncapi.yaml`), com o SHA de origem em `contratos/ORIGEM`; um teste baixa cada arquivo nesse SHA pelo GitHub e compara o checksum, e outro valida os exemplos do `platform`. `rabbitmq/` é cópia da topologia do `platform` (definitions, permissões, conf, plugins e o init de usuários) para o compose e os testes, já com uma fila de retry por atraso.

Como rodar:

```bash
make compose-up          # relay e consumidor sobem com o resto (RabbitMQ na 5672, console em 15672, usuario admin)
docker compose ps relay consumidor   # saudaveis = conectados ao broker
# fora do compose, com o MongoDB e o RabbitMQ do compose no ar e o .env carregado
ENVIRONMENT=development MP_MODE=simulado uv run python -m src.relay
ENVIRONMENT=development MP_MODE=simulado uv run python -m src.consumidor
```

## API

| Método e rota | Papéis | O que faz |
|---|---|---|
| `POST /api/v1/precos/servicos`, `PUT`/`DELETE /api/v1/precos/servicos/{codigo}` | admin | cadastro, substituição completa (`ativo` obrigatório) e desativação |
| `GET /api/v1/precos/servicos`, `GET /api/v1/precos/servicos/{codigo}` | atendente, mecânico, admin | consulta paginada (`offset` até 1.000.000, `limit` até 100) |
| `POST /api/v1/precos/pecas`, `PUT`/`DELETE /api/v1/precos/pecas/{sku}` | admin | idem, para peças |
| `GET /api/v1/precos/pecas`, `GET /api/v1/precos/pecas/{sku}` | atendente, mecânico, admin | idem, para peças |
| `POST /api/v1/precos/validacao` | mecânico, admin | `{servicos[], pecas[]}` → `{invalidos[]}` (chamada síncrona da Execução) |
| `GET /api/v1/orcamentos/{id}`, `GET /api/v1/orcamentos?ordem_id=` | atendente, admin | consulta |
| `POST /api/v1/orcamentos/{id}/decisao` | atendente, admin | `{"decisao": "aprovar" \| "recusar"}` em nome do cliente |
| `GET /api/v1/publico/orcamentos/{token}`, `POST /api/v1/publico/orcamentos/{token}/decisao` | cliente (link) | consulta e decisão sem login |
| `GET /api/v1/pagamentos/{id}` | atendente, admin | consulta, com as tentativas vistas no provedor e os estornos automáticos |
| `POST /api/v1/webhooks/mercadopago` | Mercado Pago | notificação assinada |
| `GET /simulador/checkout/{id}?token=`, `POST /api/v1/simulador/pagamentos/{id}/aprovar\|recusar?token=` | cliente (só com o simulador ligado) | checkout simulado |
| `GET /api/v1/saude` | — | liveness, com o modo do pagamento |
| `GET /api/v1/saude/pronto` | — | readiness: MongoDB respondendo e preparado em até 2 s, senão 503 |
| `GET /metrics` | — (fora da borda) | métricas Prometheus |

Erros no envelope do p3, `{"erro": {"codigo", "mensagem", "id_requisicao"}}`, inclusive o 500 (com os cabeçalhos de segurança e o `X-Request-ID`): 401 `NAO_AUTENTICADO` (mensagem única), 403 `ACESSO_NEGADO`, 404 `ENTIDADE_NAO_ENCONTRADA` (só as duas rotas públicas com token têm código próprio, `LINK_DECISAO_INVALIDO` e `CHECKOUT_NAO_ENCONTRADO`, o mesmo 404 para qualquer falha do token), 409 para regra de negócio, 422 `VALOR_INVALIDO` para invariante de domínio e 503 para dependência fora. A exceção, também herdada do p3 e igual no OS e na Execução, é o 422 de validação de schema: `{"detail": [{"type", "loc", "msg"}], "id_requisicao"}`, sem ecoar o valor recebido. A API não redireciona barra final e não anuncia o servidor.

```bash
TOKEN=...   # JWT de mecanico ou admin emitido pelo OS Service
curl -X POST localhost:8002/api/v1/precos/validacao -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"servicos": ["SRV-FREIOS"], "pecas": ["PEC-VELA", "PEC-X"]}'
# {"invalidos": ["PEC-X"]}
```

## Eventos

Os 13 eventos do catálogo do Billing (RFC-004, seção 5.3), no envelope da seção 5.2 (`id`, `tipo`, `versao`, `origem`, `correlation_id` = ordem de serviço, `causation_id`, `ocorrido_em`, `dados`): `OrcamentoGerado`, `GeracaoDeOrcamentoFalhou`, `OrcamentoAprovado`, `OrcamentoRecusado`, `OrcamentoExpirado`, `OrcamentoCancelado`, `PagamentoSolicitado`, `PagamentoConfirmado`, `PagamentoRecusado`, `PagamentoExpirado`, `PagamentoCancelado`, `PagamentoEstornado` e `EstornoDePagamentoFalhou`. Todo evento é validado contra os JSON Schemas da plataforma (cópia em `contratos/`) ao entrar na outbox, e os testes conferem um exemplo de cada. `link_decisao` e `checkout_url` carregam token e nunca vão para log.

Métricas próprias: `pytstop_mercadopago_requisicoes_total{operacao,resultado}`, `pytstop_circuit_breaker_aberto{dependencia}`, `pytstop_pagamentos_estornados_total{motivo}`, `pytstop_estornos_automaticos_recusados_total`, `pytstop_cancelamentos_de_cobranca_recusados_total`, `pytstop_webhook_assinatura_invalida_total`, `pytstop_jwks_falhas_total` e `pytstop_prazos_ultimo_ciclo_timestamp_seconds`, além de `http_request_duration_seconds{method,rota,status}` e das métricas de mensageria (seção [Mensageria](#mensageria)).

## Testes e qualidade

```bash
make check   # uv.lock em dia, ruff (lint + format), import-linter, mypy strict (src e tests), bandit e pytest com gate de 90%
make audit   # pip-audit nas dependencias de runtime
```

Unitários (domínio, matrizes de transição dos agregados, link assinado, contrato do adapter do Mercado Pago com respx, circuit breaker, JWKS com chave RSA gerada no teste, contrato das mensagens, configuração e OpenTelemetry), testes de contrato (`tests/contratos`: a cópia confere com o `platform` no SHA de origem, que o teste baixa pelo GitHub, e os exemplos de lá validam) e integração via testcontainers com MongoDB 7.0.43 real em replica set e RabbitMQ 4.3.6 com as definitions da plataforma (casos de uso, API, matriz papel × rota gerada do OpenAPI, atomicidade da outbox, idempotência, corridas reais entre decisão, webhook e expiração; relay com dois relays concorrentes e lease retomado, consumidor com origem, contrato, retry por nível e DLQ, os quatro comandos com a causa de cada resposta e o fluxo de ponta a ponta do comando publicado ao evento em `pytstop.eventos`, com o trace encadeado, a mensagem sem rota e o broker parado). O gate exige 90% de linhas e ramos (`.coveragerc`); o CI publica o resumo por pacote no summary do job `test` e o SonarQube aplica o quality gate (ADR-041).

Cobertura na versão atual (852 testes; `make test` e `python scripts/cobertura_resumo.py coverage.xml`):

| Pacote | Linhas | Cobertas | Cobertura |
|---|---:|---:|---:|
| `src` (composição, configuração, processos) | 372 | 372 | 100,0% |
| `src/compartilhado` | 789 | 789 | 100,0% |
| `src/precos` | 437 | 437 | 100,0% |
| `src/orcamento` | 628 | 628 | 100,0% |
| `src/pagamento` | 1183 | 1183 | 100,0% |
| **total** | 3409 | 3409 | 100,0% (ramos: 100,0%) |

O mesmo resumo sai no summary do job `test` de cada run do CI ([Actions → CI](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service/actions/workflows/ci.yml)); os runs de [CI](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service/actions/runs/37551387413) e [Security](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service/actions/runs/37551387409) do commit `f669aa2`, com o código desta versão, passaram nos 9 checks, inclusive o quality gate do SonarQube.
