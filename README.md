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

- **Transações e outbox:** cada caso de uso roda numa transação do MongoDB (`snapshot` + `majority`) que grava o agregado e os eventos na outbox juntos, com o envelope validado no contrato; o `_id` da mensagem é UUIDv7, a ordem de publicação do relay. Conflito de escrita (`WriteConflict`) repete o trabalho do zero relendo o estado, então decisão e expiração concorrentes nunca gravam os dois resultados. A transação tem teto de 10 s, repetições incluídas. Nos comandos da saga, ela é a transação da própria mensagem, que o consumidor abre e comita (seção [Mensageria](#mensageria)).
- **Dinheiro:** `Decimal` no domínio (VO `Dinheiro`, duas casas, até 10 dígitos inteiros, o teto do contrato das mensagens), `Decimal128` no banco e string decimal nas mensagens; nunca `float`.
- **Banco:** `python -m src.banco` (serviço `init` no compose, Job no Kubernetes) cria os índices (únicos por `ordem_id`, por orçamento e pela tentativa do provedor) e os validadores `$jsonSchema` (`moderate`) e marca a versão; API e `prazos` só conferem. Leitura tolerante a campo novo (expand/contract) e com coerência status × campos na reidratação.
- **Mercado Pago (ADR-040):** porta `GatewayPagamento` com dois adapters, escolhidos por `MP_MODE` (obrigatório, sem padrão). `MercadoPagoGateway`: Checkout Pro via httpx com timeout, retry com backoff e jitter só nas operações idempotentes, circuit breaker (5 falhas seguidas abrem por 30 s; depois, uma chamada de prova) e leitura do corpo dentro da chamada protegida (resposta fora do contrato é falha transitória). `GatewayPagamentoSimulado`: checkout próprio, só em development/test ou com `SIMULADOR_PERMITIDO=true`; o `checkout_url` leva um token assinado (HMAC, separado do link do orçamento) que as rotas do simulador exigem, com o mesmo 404 para token ausente, inválido ou expirado.
- **Webhook:** valida a `x-signature` (HMAC-SHA256 do manifesto `id:<data.id>;request-id:<x-request-id>;ts:<ts>;`) e consulta o `data.id` da query, o único que a assinatura cobre; o status vem sempre de `GET /v1/payments/{id}`, nunca do corpo. Sem `data.id` ou de outro tipo, responde 200 sem processar.
- **Tentativas e recusas:** no Checkout Pro o comprador pode tentar de novo depois de uma recusa, então cada tentativa recusada conta, e a de número `PAGAMENTO_MAX_RECUSAS` (padrão 3) encerra a cobrança como `RECUSADO`. Aprovação com valor ou moeda diferentes, ou que chega com a cobrança encerrada, é estornada na hora (chave `estorno-{pagamento_id}-{tentativa}`), registrada no agregado e publicada como `PagamentoEstornado{motivo=pagamento_apos_encerramento}`; recusa do provedor nesse estorno fica marcada no agregado, com métrica e log de erro.
- **Processo `prazos`:** a cada `PRAZOS_INTERVALO_SEGUNDOS` (30 s) concilia os pagamentos solicitados com o Mercado Pago (`GET /v1/payments/search?external_reference=`, pelo mesmo caso de uso do webhook; cobre notificação perdida) e só depois expira orçamentos e pagamentos vencidos. Encerra no fim do ciclo com SIGTERM, toca um arquivo de heartbeat a cada ciclo e expõe o instante do último ciclo em `/metrics` (porta 9100).
- **Autenticação (ADR-039):** JWT RS256 emitido pelo OS Service, validado pela chave pública do JWKS (`JWKS_URL`, timeout de 2 s, cópia fresca por 10 min; com o OS fora, a última cópia boa vale por até 1 h e 3 falhas seguidas abrem um circuit breaker por 30 s), conferindo assinatura, `iss` (`pytstop-os-service`), `aud` (`pytstop`), `exp`, `sub` (UUID do usuário), `type=access` e `papel`, com 10 s de tolerância de relógio. Toda falha de credencial responde o mesmo 401; papel válido sem permissão, 403; JWKS indisponível e sem cópia, 503 com `Retry-After`.
- **Link de decisão:** token HMAC do orçamento e do prazo (segundo cheio, igual ao `valido_ate`), sem login. Token inválido ou expirado, orçamento inexistente ou já decidido: o mesmo 404, na consulta e na decisão.

Proveniência: `src/compartilhado` parte do p3 (o PytStop da fase 3) [em `08dcffe`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/08dcffe6365ece594f438cdbc4c5eef1d88ebfb1): base de entidades e eventos, unidade de trabalho, outbox, logging JSON com mascaramento de PII, envelope de erro, middleware de cabeçalhos, métricas, padrões de teste, Dockerfile e Makefile. Do relay do p3 vieram os atrasos entre tentativas (1, 4, 16 e 64 s, `dead` na quinta), o lease, o fencing e o heartbeat em arquivo; o relay em si foi reescrito para MongoDB e RabbitMQ. Ficaram de fora o SQLAlchemy (o Billing usa MongoDB), a UI e o rate limiting da aplicação (no cluster ele fica no Kong). O `catalogo_servicos` do p3 virou a tabela de preços, com código de negócio estável e preço comercial de peça.

## Participação na saga

| Comando (OS → Billing) | Resposta (evento) | Repetição do comando |
|---|---|---|
| `GerarOrcamento` | `OrcamentoGerado{linhas, total, valido_ate, link_decisao}` ou `GeracaoDeOrcamentoFalhou{motivo, codigos_invalidos}` (diagnóstico vazio, código inexistente ou inativo, quantidade fora de 1 a 1000, total acima do teto) | republica o `OrcamentoGerado` registrado; a falha não grava nada, então a repetição reavalia e responde de novo |
| `CancelarOrcamento` | `OrcamentoCancelado` | orçamento já cancelado, recusado ou expirado responde `OrcamentoCancelado` sem mudar nada |
| `SolicitarPagamento` | `PagamentoSolicitado{valor, checkout_url, expira_em}` (só orçamento aprovado) | republica o registrado, sem nova cobrança |
| `EstornarPagamento` | cobrança aberta: fecha o checkout no provedor e responde `PagamentoCancelado` (se o provedor recusar fechar, o cancelamento conclui do mesmo jeito, com métrica e log, e a aprovação tardia é estornada); paga: estorna (`X-Idempotency-Key = estorno-{pagamento_id}`) e responde `PagamentoEstornado{motivo=compensacao}`, ou `EstornoDePagamentoFalhou` se o provedor recusar o estorno; recusada, expirada ou cancelada: `PagamentoCancelado` (nada a devolver); já estornada pelo estorno automático: `PagamentoEstornado{motivo=pagamento_apos_encerramento}`, o desfecho registrado | republica o desfecho registrado, sem chamar o provedor |

Toda resposta leva como `causation_id` o `id` do comando respondido, inclusive o desfecho republicado para o comando repetido (pelo mesmo `id` ou por `id` novo com a mesma ordem, como no reenvio do orquestrador). Repetição não é recusa: a compensação repetida também republica o desfecho registrado (RFC-004, seções 4.5 e 7.1). As compensações acham o recurso pelo `ordem_id` (os ids são opcionais).

Comando que o domínio recusa, e que não é repetição, tem três destinos (ADR-036):

- **Descompasso de estado**, como `SolicitarPagamento` com o orçamento não aprovado: o consumidor confirma o consumo ao broker (ack) e registra o log `command_ignored`, com o motivo em código, e `resultado="ignorada"` na métrica, sem resposta. A saga segue pelo evento que já recebeu ou pelo prazo.
- **Falha de negócio com evento no contrato:** responde o evento (`GeracaoDeOrcamentoFalhou`, `EstornoDePagamentoFalhou`).
- **Falha permanente sem evento no contrato:** vai para a fila de mensagens mortas (DLQ, *dead letter queue*), que alerta o operador, e o orquestrador compensa pelo prazo técnico. É o `SolicitarPagamento` com orçamento ausente ou de outra ordem, ou com a cobrança recusada pelo provedor.

Provedor fora do ar não é recusa: o comando volta pela fila de retry. **Lápide:** a compensação que chega antes do comando original (passo em voo, RFC-004 seção 4.5) grava o agregado já encerrado (orçamento `CANCELADO` sem linhas, pagamento `CANCELADO` sem cobrança) e responde; o original, quando chegar, encontra a lápide pelo índice único de `ordem_id` e é descartado.

Fatos que não vêm de comando: `OrcamentoAprovado`/`OrcamentoRecusado` (pelo link ou pelo atendente, com `decidido_por` = `sub` do atendente), `OrcamentoExpirado`, `PagamentoConfirmado`, `PagamentoRecusado`, `PagamentoExpirado` e o `PagamentoEstornado` do estorno automático. O `causation_id` deles é o comando que abriu o fluxo (`GerarOrcamento` para os do orçamento, `SolicitarPagamento` para os do pagamento), guardado no documento em `aberto_por` com o contexto de trace, e o evento sai como filho desse contexto, com um *span link* para o trace de quem retomou o passo: a saga segue num trace só (ADR-043). O processo `prazos` abre um span por registro que expira ou concilia; a requisição HTTP entra no link quando a API ganhar a instrumentação do FastAPI.

O estorno automático (aprovação tardia, de valor ou moeda diferentes, ou segunda tentativa paga do mesmo checkout) publica `PagamentoEstornado{motivo=pagamento_apos_encerramento}` com o pagamento em qualquer estado: `RECUSADO`, `EXPIRADO` e `CANCELADO` passam a `ESTORNADO`, enquanto `SOLICITADO`, `CONFIRMADO` e `ESTORNADO` ficam como estão. Esse fato leva no `causation_id` o id do `SolicitarPagamento`. O mesmo `motivo` aparece na resposta ao `EstornarPagamento` que chega depois do estorno automático, mas com o id do comando no `causation_id`. A saga reconhece a resposta ao `EstornarPagamento` pelo `causation_id`, de qualquer `motivo`; o estorno automático só atualiza o resumo do pagamento na OS (ADR-040, passo 7; RFC-004, seção 5.2).

## Como rodar

Requisitos: Python 3.14 com [uv](https://docs.astral.sh/uv/) e Docker.

```bash
make compose-up
curl localhost:8002/api/v1/saude
curl localhost:8002/api/v1/saude/pronto
make compose-logs
make compose-down
make smoke
```

O `make compose-up` sobe o init do banco, a API (porta 8002), o `prazos`, o relay, o consumidor, o MongoDB e o RabbitMQ, e cadastra os preços de demonstração. A primeira chamada ao `curl` é a liveness (`{"status": "ok", "modo": "simulado"}`) e a segunda, a readiness (MongoDB respondendo e preparado). O `make compose-logs` segue os logs da API, do `prazos`, do relay e do consumidor, e o `make compose-down` derruba tudo e apaga os volumes. O `make smoke` é o mesmo smoke do CI, em projeto e portas próprios, e inclui um comando de ponta a ponta.

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
| `PRAZOS_INTERVALO_SEGUNDOS`, `PRAZOS_HEARTBEAT`, `METRICS_PORT` | `30`, `/tmp/prazos-heartbeat`, `9100` | processo `prazos` (`METRICS_PORT`, a porta do `/metrics`, vale para os três processos sem API, como no OS e na Execução) |
| `RABBITMQ_URL` | sem padrão | broker com o usuário do serviço (`amqp://billing:<senha>@<host>:5672/%2F`), exigido pelo relay e pelo consumidor; o usuário vai no `user_id` de toda publicação |
| `RELAY_HEARTBEAT`, `CONSUMIDOR_HEARTBEAT` | `/tmp/relay-heartbeat`, `/tmp/consumidor-heartbeat` | arquivo de vida do relay e do consumidor |
| `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME` | `false`, `http://jaeger:4317`, `billing-service` | exportação dos traces do relay, do consumidor e do `prazos` pelo protocolo do OpenTelemetry (OTLP) sobre gRPC; desligada, o contexto de trace segue nas mensagens do mesmo jeito, nos headers `traceparent`/`tracestate` do padrão W3C Trace Context |
| `CONTRATOS_DIR` | `contratos/` do repositório (`/app/contratos` na imagem) | schemas das mensagens |
| `RUN_SEED_ON_STARTUP` | `false` (o compose liga) | seed idempotente da tabela de preços no boot da API |
| `ROOT_PATH` | vazio | prefixo da borda (`/billing` no Kubernetes), passado ao `--root-path` do uvicorn: o Swagger de `/billing/docs` busca o `/billing/openapi.json`, e o OpenAPI leva o prefixo em `servers` (ADR-038) |

Processos da imagem (`entrypoint.sh`): `api` (padrão), `prazos`, `relay`, `consumidor`, `banco` (preparação idempotente do MongoDB, antes dos outros) e `aguarda-banco` (espera essa preparação; seção [Implantação](#implantação)). A imagem roda como o usuário 1001, sem shell de login, com o sistema de arquivos só leitura no compose e no Kubernetes.

## Mensageria

RabbitMQ 4.3.6 com a topologia da plataforma (ADR-036; RFC-004, seções 5.1 a 5.5): o serviço só faz declaração passiva, e cada processo confere apenas o que o usuário `billing` alcança.

| | Fila ou exchange | Mensagens |
|---|---|---|
| Consome | `billing.comandos` (ligada a `comando.billing.#` no `pytstop.comandos`) | `GerarOrcamento`, `CancelarOrcamento`, `SolicitarPagamento` e `EstornarPagamento`, publicados pelo usuário `os` |
| Publica | `pytstop.eventos`, routing key `evento.billing.<tipo em snake_case>` | os 13 eventos da seção [Eventos](#eventos), com o usuário `billing` |
| Retry | `pytstop.retry`, routing key `billing.comandos.retry.<1s\|5s\|15s\|60s\|300s>` | cópia do comando com `x-tentativa` de 1 a 5 |

- **Envelope:** `id`, `tipo`, `versao` (1), `origem` (`billing-service`), `correlation_id` (a ordem de serviço), `causation_id`, `ocorrido_em` (UTC, do relógio injetado nos casos de uso) e `dados`. Nas propriedades do AMQP (o protocolo do RabbitMQ) vão `message_id`, `correlation_id`, `type`, `user_id`, `content_type` e `delivery_mode=2`, com os headers `traceparent`/`tracestate`. O envelope é validado no JSON Schema do contrato ao gravar na outbox (fora do contrato é defeito deste serviço e aborta a transação) e ao consumir (erro permanente).
- **Outbox e relay** (`python -m src.relay`):
  - Cada documento da outbox leva o envelope, o exchange, a routing key e o `traceparent` de quem gravou.
  - O relay reivindica uma linha por vez com `find_one_and_update`: a pendente, ou a em entrega com lease vencido, passa a em entrega por 30 s com um token de reivindicação novo. Toda marcação exige o token, então um relay atrasado não marca como entregue o que outro retomou, e mais de um relay pode rodar ao mesmo tempo.
  - A publicação usa `mandatory` e espera a confirmação do broker (publisher confirms). Devolução sem rota, recusa do broker (nack) ou canal fechado por permissão contam tentativa, com os atrasos do p3 até `dead` na quinta, e o canal de publicação é reaberto para a mensagem seguinte.
  - Queda do broker devolve a linha sem gastar tentativa. Sem conexão, o relay não reivindica nada e reconecta com backoff até 30 s, com jitter.
  - Uma linha em backoff não segura as seguintes da mesma OS: a ordem de consumo não é garantida de qualquer forma, e quem decide é a etapa da saga (ADR-036).
- **Consumidor** (`python -m src.consumidor`):
  - Prefetch 1 e confirmação manual (ack). Uma mensagem que derruba a conexão a cada entrega (um header que a biblioteca AMQP não decodifica) vai para a DLQ pelo limite de 5 entregas da fila, sem levar junto as que estariam no mesmo lote.
  - Antes de ligar o log e o span aos ids do envelope, confere o tamanho do corpo (até 64 KiB), o JSON, o envelope no contrato e o `user_id` contra o produtor do tipo: comandos são do `os`, e a cópia de retry chega com o próprio `billing` e `x-tentativa` maior que zero. A mensagem descartada nessa etapa vai para o log só com as propriedades AMQP que a acham na DLQ, cortadas em 64 caracteres.
  - O handler roda numa thread de trabalho, para a conexão seguir atendendo o heartbeat do broker, e dentro da transação da mensagem: o consumidor a abre e comita, e o efeito, as respostas na outbox e o `id` da mensagem em `mensagens_processadas` gravam juntos ou nada grava. O caso de uso grava pelo `executar` da unidade de trabalho, sem comitar.
  - A transação só começa no servidor no primeiro `executar`: o que o caso de uso lê e pede ao provedor antes dele fica fora dela. O teto de 10 s, porém, conta desde o começo do handler, repetições incluídas, com a chamada ao provedor dentro dele; estourado, é erro transitório, e a mensagem segue pela fila de retry.
  - O mesmo `id` de novo passa pelo caso de uso, que é idempotente pela ordem e republica o desfecho registrado (conta como `duplicada`). O original atrasado depois da lápide é descartado sem resposta (`ignorada`).
  - Erro transitório (provedor ou rede fora, banco inacessível ou erro que o próprio MongoDB marca como repetível) publica a cópia na fila de retry do nível da nova tentativa. A cópia sai sem `expiration`: o atraso é o TTL (*time to live*) da própria fila, que a devolve a `billing.comandos`. Ela vai num canal de publicação separado do de consumo, e a original só recebe ack depois da confirmação da cópia.
  - A sexta falha, a cópia recusada e qualquer outro erro (corpo, contrato, tipo, versão, `user_id`, falha de negócio sem evento no contrato ou defeito) vão direto para `billing.comandos.dlq` (`reject` sem requeue).
  - Mensagem na DLQ volta à fila depois de corrigida a causa: no cluster, `make -C platform redrive FILA=billing.comandos` (repositório `platform`) move as mensagens de `billing.comandos.dlq` para `billing.comandos` por um shovel do próprio broker, que preserva as propriedades (ADR-036). O [runbook da saga](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/operacao/runbook-saga.md) do `platform` descreve o diagnóstico antes do redrive.
- **Retenção por índice TTL:** linhas entregues da outbox somem 7 dias depois (`entregue_em`, só com `status: entregue`) e `mensagens_processadas`, 30 dias depois. As linhas `dead` (alerta pela métrica `outbox_dead`) ficam 30 dias (`morta_em`) para investigação.
- **Reprocesso de uma linha `dead`:** corrigida a causa (o `ultimo_erro` diz a exceção; envelope fora do contrato não se conserta repetindo), devolva uma linha por vez ao relay, no `mongosh` com o banco `billing` (rodando com `--file`, como no runbook, envolva cada expressão em `printjson(...)` para ver o resultado). Não há rota administrativa nem log de auditoria: anote no ticket do incidente o `_id`, o `tipo`, o `envelope.correlation_id`, quem reprocessou e por quê. O [runbook da saga](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/operacao/runbook-saga.md#linha-dead-na-outbox) traz o `kubectl exec` completo.

  Primeiro, a leitura:

  ```javascript
  db.getSiblingDB("billing").outbox.find({status: "dead"}, {tipo: 1, routing_key: 1, tentativas: 1, ultimo_erro: 1, morta_em: 1, "envelope.correlation_id": 1}).toArray()
  ```

  Depois, a devolução, com o `_id` da leitura no lugar do UUID de exemplo:

  ```javascript
  db.getSiblingDB("billing").outbox.updateOne(
    {_id: UUID("018f3b0e-7c1a-7d2e-9b4f-0123456789ab"), status: "dead"},
    {$set: {status: "pendente", tentativas: 0, proxima_tentativa_em: new Date()}, $unset: {morta_em: "", ultimo_erro: ""}})
  ```

  `matchedCount: 1` é o sinal de que a linha estava `dead` e voltou a `pendente`; `matchedCount: 0` quer dizer que o `_id` não existe ou a linha já não estava `dead`, e nada mudou. O filtro `status: "dead"` é a guarda: sem ele, o comando devolveria ao relay uma linha já `entregue` (ou `pendente`) e a mensagem sairia de novo.
- **Rastreamento (ADR-043):** o relay publica num span de produtor (`PRODUCER`) filho do contexto gravado na outbox, com *span link* para quem retomou o passo quando a linha o traz, e injeta o seu no header. O consumidor abre o span de consumidor (`CONSUMER`) filho da publicação, e a outbox gravada nele leva esse contexto adiante. Os logs JSON levam `correlation_id`, `trace_id` e `span_id`. Laço ocioso não abre span.
- **Métricas** (porta `METRICS_PORT` de cada processo): `pytstop_mensagens_publicadas_total{tipo}`, `pytstop_mensagens_consumidas_total{tipo,resultado}` (`processada`, `duplicada`, `ignorada`, `retry`, `dlq`) e, no relay, `outbox_pendentes` e `outbox_dead`, os nomes do p3.
- **Saúde:**
  - Relay e consumidor tocam um arquivo a cada volta do laço (`RELAY_HEARTBEAT`, `CONSUMIDOR_HEARTBEAT`), com `pronto` só enquanto a conexão com o broker está de pé; fora dela, inclusive na espera antes de reconectar, o arquivo diz `conectando`. A liveness confere a idade do arquivo, e a readiness, também o conteúdo.
  - É um arquivo só, e não o par heartbeat e pronto do OS e da Execução, porque a mesma sonda lê os dois sinais de uma vez. O `prazos`, sem conexão para provar, só toca o arquivo dele.
  - A reconexão espera de 1 a 30 s, dobrando, com jitter: entre a metade e o total do atraso, para as réplicas que perderam o broker juntas não voltarem juntas. Vale também para a conexão que cai logo depois de aberta, para o consumidor cancelado pelo broker e para o nome do broker que deixa de resolver no DNS (o Service headless do RabbitMQ some do DNS sem pod pronto, no boot ou depois de uma queda): o processo fica fora da prontidão, com o log `broker_unavailable`, em vez de cair e reiniciar. Na abertura da conexão, contam como broker fora o erro de conexão do pika (`AMQPError`), o nome sem resolução (`socket.gaierror`) e o broker que aceita a conexão TCP e não responde o AMQP no prazo da pilha do pika (15 s, `AMQPConnectorStackTimeout`); qualquer outro erro derruba o processo, para o orquestrador reiniciá-lo e o defeito aparecer: falta de descritores, certificado TLS recusado e erro de disco ao tocar o arquivo de vida.
  - Com o DNS mudo (sem resposta, nem a de nome inexistente), a consulta do pika não tem prazo e prende a tentativa pelo tempo do resolver: 20 s medidos com a glibc e o `resolv.conf` padrão do Kubernetes (`ndots:5`, três domínios de busca), que o `timeout`, o `attempts` e a lista de domínios multiplicam. O arquivo de vida é tocado de novo quando a tentativa falha, então a idade dele chega, no máximo, ao maior entre esse tempo e a espera do backoff (30 s), e não à soma dos dois. O SIGTERM só é atendido quando a consulta volta (medido: sinal aos 3 s, laço encerrado aos 20 s), sem conexão nem mensagem em curso para perder; contra o broker que aceita a conexão e não responde, o limite é o prazo da pilha do pika (15 s medidos). A sonda de liveness (60 s no compose e 90 s no Kubernetes) e o `terminationGracePeriodSeconds` dos manifests (45 s) têm margem sobre o tempo do resolver; o encerramento do Docker (10 s) interrompe o processo à força.
  - Com alarme de memória ou disco no broker, a conexão que publica fica bloqueada. O relay não reivindica linha nova enquanto ela estiver bloqueada, e o pika a derruba em 8 s, mesmo ociosa (abaixo do prazo de encerramento do Docker e do Kubernetes): o publish preso cai junto, com a linha devolvida sem gastar tentativa, e o relay reconecta com backoff.
- **Encerramento:** SIGTERM termina a mensagem em curso, cancela o consumo (as mensagens pré-buscadas voltam à fila) e fecha as conexões. API, `prazos` e consumidor não sobem sem os schemas dos contratos.
- **Contratos:** `contratos/` é cópia do `platform` no commit gravado em `contratos/ORIGEM`: os schemas e exemplos das mensagens do Billing, o `asyncapi.yaml` e, em `contratos/rabbitmq/`, a topologia do broker (definitions, permissões, configuração, plugins e o script que cria os usuários) que o compose e os testes sobem. Um teste baixa o `platform` nesse commit e compara os arquivos byte a byte (marcador `rede`: sem acesso ao GitHub, falha no CI e é pulado fora dele); outro valida os exemplos do `platform`.

Como rodar:

```bash
make compose-up
docker compose ps relay consumidor
cp .env.example .env
uv run --env-file .env python -m src.relay
METRICS_PORT=9101 uv run --env-file .env python -m src.consumidor
```

O `make compose-up` sobe o relay e o consumidor com o resto (RabbitMQ na 5672, console em 15672, usuário `admin`), e o `docker compose ps` os mostra saudáveis quando conectaram ao broker. Os três últimos comandos rodam fora do compose, com o MongoDB e o RabbitMQ do compose no ar (o `uv run` não lê o `.env` sozinho): o relay num terminal e o consumidor em outro, com outra porta de métricas.

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

Erros no envelope do p3, `{"erro": {"codigo", "mensagem", "id_requisicao"}}`, inclusive o 500 (com os cabeçalhos de segurança e o `X-Request-ID`): 401 `NAO_AUTENTICADO` (mensagem única), 403 `ACESSO_NEGADO`, 404 `ENTIDADE_NAO_ENCONTRADA` (só as duas rotas públicas com token têm código próprio, `LINK_DECISAO_INVALIDO` e `CHECKOUT_NAO_ENCONTRADO`, o mesmo 404 para qualquer falha do token), 409 para regra de negócio, 422 `VALOR_INVALIDO` para invariante de domínio e 503 para dependência fora. A exceção, também herdada do p3 e igual no OS e na Execução, é o 422 de validação de schema: `{"detail": [{"type", "loc", "msg"}], "id_requisicao"}`, sem ecoar o valor recebido. A API não redireciona barra final, não anuncia o servidor e escreve um access log estruturado (`http_request`: método, caminho, status, duração e `request_id`, com o token do link de decisão mascarado) no lugar da linha de texto do uvicorn.

Com `TOKEN` um JWT de mecânico ou admin emitido pelo OS Service, a chamada abaixo responde `{"invalidos": ["PEC-X"]}`:

```bash
curl -X POST localhost:8002/api/v1/precos/validacao -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"servicos": ["SRV-FREIOS"], "pecas": ["PEC-VELA", "PEC-X"]}'
```

## Eventos

Os 13 eventos do catálogo do Billing (RFC-004, seção 5.3), no envelope da seção 5.2 (`id`, `tipo`, `versao`, `origem`, `correlation_id` = ordem de serviço, `causation_id`, `ocorrido_em`, `dados`): `OrcamentoGerado`, `GeracaoDeOrcamentoFalhou`, `OrcamentoAprovado`, `OrcamentoRecusado`, `OrcamentoExpirado`, `OrcamentoCancelado`, `PagamentoSolicitado`, `PagamentoConfirmado`, `PagamentoRecusado`, `PagamentoExpirado`, `PagamentoCancelado`, `PagamentoEstornado` e `EstornoDePagamentoFalhou`. Todo evento é validado contra os JSON Schemas da plataforma (cópia em `contratos/`) ao entrar na outbox, e os testes conferem um exemplo de cada. `link_decisao` e `checkout_url` carregam token e nunca vão para log.

Métricas próprias: `pytstop_mercadopago_requisicoes_total{operacao,resultado}`, `pytstop_circuit_breaker_aberto{dependencia}`, `pytstop_pagamentos_estornados_total{motivo}`, `pytstop_estornos_automaticos_recusados_total`, `pytstop_cancelamentos_de_cobranca_recusados_total`, `pytstop_webhook_assinatura_invalida_total`, `pytstop_jwks_falhas_total` e `pytstop_prazos_ultimo_ciclo_timestamp_seconds`, além de `http_request_duration_seconds{method,rota,status}` e das métricas de mensageria (seção [Mensageria](#mensageria)).

## Testes e qualidade

```bash
make check
make audit
```

O `make check` confere o `uv.lock`, o ruff (lint e format), o import-linter, o mypy strict (`src` e `tests`), o bandit e o pytest com o gate de 90%; o `make audit` roda o pip-audit nas dependências de runtime.

- **Unitários:** domínio, matrizes de transição dos agregados, link assinado, contrato do adapter do Mercado Pago com respx, circuit breaker, JWKS com chave RSA gerada no teste, contrato das mensagens, configuração e OpenTelemetry.
- **Contrato** (`tests/contratos`): a cópia confere com o `platform` no SHA de origem, que o teste baixa pelo GitHub, e os exemplos de lá validam.
- **Integração** via testcontainers, com MongoDB 7.0.43 real em replica set e RabbitMQ 4.3.6 com as definitions da plataforma:
  - API e casos de uso: matriz papel × rota gerada do OpenAPI, atomicidade da outbox, idempotência e corridas reais entre decisão, webhook e expiração.
  - Mensageria: relays concorrentes e lease retomado; consumidor com origem, contrato, entrada hostil, retry por nível e DLQ; os quatro comandos com a causa de cada resposta; e o fluxo de ponta a ponta do comando publicado ao evento em `pytstop.eventos`, com o trace encadeado, a mensagem venenosa, a mensagem sem rota, o alarme de memória e o broker parado.
- **Gate:** 90% de linhas e ramos (`.coveragerc`); o SonarQube aplica o quality gate (ADR-041). O resumo da cobertura (o número do gate, que soma linhas e ramos, e o detalhe por pacote) sai de `make test` seguido de `uv run python scripts/cobertura_resumo.py coverage.xml`, e o CI publica o mesmo resumo no summary do job `test` de cada run ([Actions → CI](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service/actions/workflows/ci.yml)).
- **Resumos no CI:** além do log do passo, o resultado do `pip-audit` e do `trivy` sobre a imagem do serviço vai para o summary dos jobs `pip-audit` e `trivy` do `security.yml` (`scripts/ci/registra-no-summary.sh`, que mantém o código de saída da ferramenta), ao lado do relatório das imagens de terceiros.

## Implantação

Manifests do Kubernetes em [`k8s/`](k8s): o `base` e um overlay por ambiente, validados no job `build` do CI por `make manifests` (kubeconform com os schemas do Kubernetes 1.35, a versão do nó do kind, e `Secret` reprovado; trivy config sem achado HIGH ou CRITICAL, com a única exceção e o motivo em [`k8s/trivy-ignore.rego`](k8s/trivy-ignore.rego); e uma conferência de que todo campo de senha do `replica-set.js` vem de `segredo(...)`, que a exceção do trivy não enxerga). O gitleaks varre o histórico do commit testado, com regras próprias para URI com senha (MongoDB e AMQP) e o teste que as faz reprovar um manifesto com a senha gerada ([`scripts/ci/testa-gitleaks.sh`](scripts/ci/testa-gitleaks.sh)). Namespace `pytstop-billing`, prefixo `/billing` no Kong (ADR-038, ADR-042).

| Arquivo em `k8s/base` | O que tem |
|---|---|
| `namespace.yaml` | O namespace, com Pod Security `restricted` em `enforce` |
| `api.yaml`, `relay.yaml`, `consumidor.yaml`, `prazos.yaml` | Um Deployment por processo da imagem, cada um com o initContainer `aguarda-banco` |
| `servico.yaml` | Service interno `billing-service:8000`, o `BILLING_URL` da Execução |
| `banco.yaml` | MongoDB 7.0.43 em replica set de um nó (`rs0`), com keyfile e volume, e o `mongodb_exporter` em sidecar |
| `job.yaml`, `replica-set.js` | Job `billing-inicializacao`: replica set e usuários, índices e validadores, seed de preços |
| `hpa.yaml` | HPA da API por CPU (70% do request) |
| `networkpolicy.yaml` | Entrada negada por padrão e liberada só onde precisa |
| `borda.yaml` | Services e Ingress do Kong, cópia do exemplo do `platform` |

| | `kind` | `kind-ci` (CD) | `k3s` |
|---|---|---|---|
| Réplicas da API (HPA) | 1 a 2 | 1 | 1 a 3 |
| URL pública (`BILLING_PUBLIC_URL`) | `https://localhost/billing` | `https://localhost/billing` | a da VM, gravada pelo deploy do k3s |
| Prazos | padrão | orçamento vence em 72 s, `prazos` a cada 5 s e a primeira recusa do provedor encerra a cobrança (E2E) | padrão |
| Volume do MongoDB | 1Gi | 1Gi | 5Gi |
| Imagem | a do commit atual, num overlay gerado (`make kind-deploy`) | a do CD, num overlay gerado | por digest, num overlay gerado |

- **Ordem de subida:** o `implantar-servicos.sh` do `platform` apaga o Job anterior (Job é imutável), aplica o overlay com `apply --server-side` e espera, nesta ordem, o MongoDB, o Job completar e cada Deployment.
- **Job de inicialização:** roda em três etapas. O initContainer `replica-set` (`mongosh`) espera o `mongod`, inicia o replica set e cria os usuários `billing` e `exporter`, como root. O `banco` cria índices e validadores e marca a versão (`python -m src.banco`, com o usuário `billing`). O `seed` cadastra os preços de demonstração que faltam.
- **`aguarda-banco` e rollback:** os Deployments ficam no initContainer `aguarda-banco` (`python -m src.banco aguardar`) até a versão deles estar marcada: no rolling update, os pods novos esperam o Job da versão nova enquanto os antigos seguem servindo. Banco à frente da imagem não é espera: depois de um `kubectl rollout undo`, o pod revertido sobe sobre a versão que o Job novo marcou, e o rollback não desce a versão do banco (mudanças aditivas).
- **Configuração:** o ConfigMap `billing-service` (com o hash do conteúdo no nome: mudar um valor reinicia os pods) entra por `envFrom`, com `ENVIRONMENT=production` também no kind, para as guardas de boot valerem com os segredos gerados.
- **Segredos:** nenhum está nos manifests. O `make deploy` do `platform` os gera ([Segredos gerados](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform#segredos-gerados)), e eles entram por `secretKeyRef`; a `MONGODB_URI` se monta no pod por expansão de variável (`mongodb://billing:$(MONGO_BILLING_PASSWORD)@billing-mongo-0.billing-mongo...`), e o `kubectl describe` mostra o molde, não a senha. O molde está escrito uma vez, no container `api` de `api.yaml`, e o `replacements` de `k8s/base/kustomization.yaml` o copia para os outros processos e o Job (`make manifests` reprova se alguma cópia ficar com o valor sentinela).

  | Secret | Chaves | Quem lê |
  |---|---|---|
  | `billing-mongo` | `MONGO_INITDB_ROOT_PASSWORD`, `MONGO_BILLING_PASSWORD`, `MONGO_EXPORTER_PASSWORD`, `MONGO_KEYFILE` | o `mongod` (o root no primeiro boot do volume e o keyfile), o Job (root e `billing`), os processos (`billing`) e o exporter (`exporter`) |
  | `billing-link` | `ORCAMENTO_LINK_SECRET` | API e consumidor |
  | `rabbitmq` | `RABBITMQ_URL`, com o usuário `billing` | relay e consumidor |
  | `billing-mercadopago` (opcional) | `MP_ACCESS_TOKEN`, `MP_WEBHOOK_SECRET` | API, consumidor e `prazos`, com `MP_MODE=mercadopago` (no k3s, quando o Secret existe) |

- **Sondas da API:** liveness em `/api/v1/saude` e readiness em `/api/v1/saude/pronto` (MongoDB e versão do banco). A startup confere a mesma `/api/v1/saude` a cada 2 s, por até 60 s, e a liveness só vale depois dela: o primeiro 200 leva cerca de 10 s a 0,1 CPU, o request, e mais num runner disputado. O `terminationGracePeriodSeconds` é de 30 s.
- **Sondas dos processos:** relay e consumidor usam o arquivo de vida em `/tmp`: liveness com ele tocado há menos de 90 s e readiness com o conteúdo `pronto`. A startup (até 60 s) confere a idade, e não o `pronto`, para o broker fora no boot deixar o pod fora da prontidão em vez de reiniciá-lo. No `prazos`, a startup e a liveness conferem que o arquivo tem menos de 120 s (quatro ciclos do padrão), e a readiness, só que ele existe; como o `/tmp` sobrevive ao restart do contêiner, a startup só barra o arquivo com mais de 120 s (o restart pela liveness), e depois de um crash ou OOM com o arquivo mais novo o pod volta a `Ready` até o processo novo concluir o primeiro ciclo. As sondas exec rodam `python -c`, sem shell, e o `terminationGracePeriodSeconds` é de 45 s, com margem sobre a consulta de DNS que o SIGTERM não interrompe.
- **Sonda do MongoDB:** um `ping` pelo `mongosh`, que leva cerca de 1 s de CPU para subir: a liveness roda a cada 20 s, a readiness a cada 10 s, e as duas aceitam 10 s.
- **Usuários do MongoDB:** `root` (só no primeiro boot do volume e no Job), `billing` (`readWrite` e `dbAdmin` em `billing`, nos processos e no Job; o `dbAdmin` serve ao `collMod` dos validadores a cada deploy) e `exporter` (só `clusterMonitor`, que não lê as coleções de `billing`, mas inclui o oplog em `local`, onde ficam as escritas de `billing`: risco aceito de demonstração).
- **Imagens de terceiros:** `mongo:7.0.43` e `percona/mongodb_exporter:0.53.0`, por tag fixa e as da tabela de versões do `platform`. O job `trivy` do `security.yml` lista no summary de cada PR e da execução semanal os HIGH e CRITICAL com correção disponível das duas, sem reprovar o job. Risco aceito: na `mongo` quase todos estão em binários Go que o pod não executa (`gosu`, `mongodump` e as outras ferramentas da imagem), mais o OpenSSL e o `mongosh`; o exporter executa o binário dele, mas só o Prometheus alcança a porta 9216 (NetworkPolicy) e ele só fala com o `mongod` do próprio pod. A correção depende de um build novo do fornecedor, então a política é subir a versão a cada release do serviço, junto da tabela do `platform`.
- **Métricas:** todo pod de processo é anotado para o Prometheus da plataforma (`prometheus.io/scrape`, `port` e `path`): a API na porta `http` (8000), relay, consumidor e `prazos` na `metrics` (9100) e o MongoDB pelo exporter (9216), com `--collector.dbstats` e `--collector.replicasetstatus`; o `mongodb_up` diz se o exporter entrou no banco. O exporter usa um pool de conexões só (`--mongodb.global-conn-pool`) e o `mongod` sobe com `--quiet`: sem isso cada scrape abria e autenticava uma conexão e cada sonda abria uma, e o log do banco passava de mil linhas em 10 minutos. O `--quiet` tira as linhas "Connection accepted" e "Connection ended" e os registros dos comandos `drop` e `dropIndexes` (`CMD: drop` e `CMD: dropIndexes`); o `drop` segue visível em "Finishing collection drop" e "dropDatabase", com o namespace. A origem da conexão ("client metadata"), a autenticação que deu certo ou falhou, a query lenta, a falha de autorização, o aviso e o erro seguem no log. Sobra uma linha "Checking authorization failed" por scrape (uma a cada 15 s, cerca de 5.760 por dia): é o próprio exporter, com o papel `clusterMonitor`, perguntando em `admin.system.version` se o nó é de shard (`shardIdentity`), e não sinal de falha de autorização real; a real é a que não traz `shardIdentity` no comando.
- **Rede e endurecimento:** a NetworkPolicy só trata a entrada. Tudo é negado, e ficam liberados o Kong e o Prometheus na API, a API da Execução na API (validação de preços), o Prometheus nas métricas dos processos e do exporter e os pods do próprio namespace no MongoDB. Todo pod roda sem root (1001; 999 no MongoDB; 65534 no exporter), sem escalada de privilégio, sem capability, com seccomp `RuntimeDefault`, sem token de ServiceAccount e com a raiz somente leitura (`/tmp` num `emptyDir`).
- **Recursos** (request e limit): API 100m e 500m de CPU, 256 e 512 Mi de memória; relay, consumidor e `prazos` 50m e 250m, 128 e 256 Mi; MongoDB 100m e 500m, 256 Mi e 1 Gi (cache do WiredTiger em 0,25 GB); exporter 10m e 100m, 32 e 64 Mi; Job 50m e 500m, 128 e 256 Mi.
- **Borda:** no kind, o Kong publica o serviço em `https://localhost/billing`, com o certificado padrão dele (`curl -k`): `/billing/api/v1/*`, o Swagger em `/billing/docs` (a API sobe com `ROOT_PATH=/billing`), o link de decisão, o webhook e o simulador, os três com limite próprio; `/billing/metrics` e `/billing/api/v1/admin/*` respondem 404 sem chegar ao serviço.

### No kind local

Com o repositório `platform` clonado ao lado deste:

```bash
make -C ../postech-sw-arch-p4-platform kind-up deploy
make kind-deploy
../postech-sw-arch-p4-platform/scripts/ci/smoke-servicos.sh billing-service
curl -k https://localhost/billing/api/v1/saude
make manifests
```

O primeiro comando cria o cluster, sobe a plataforma e gera os Secrets. O `make kind-deploy` constrói a imagem do commit atual, carrega no kind e aplica o overlay `kind`; com mudança não commitada a tag vira `<commit>-sujo-<hash>`, de modo que um novo `make kind-deploy` depois de editar o código também reinicia os pods. O `smoke-servicos.sh` confere o Job, os Deployments, a borda, o `up` de cada pod no Prometheus e a NetworkPolicy; o `curl` pede a saúde pela borda; e o `make manifests` roda o kubeconform e o trivy config, como no CI.

`make kind-deploy KIND_OVERLAY=kind-ci` usa o overlay do CD (o nome antigo, `OVERLAY`, também vale), e `PLATFORM_DIR=<caminho>` aponta outro clone do `platform` (os mesmos nomes do Execution Service). Para subir os três serviços de uma vez, o `implantar-servicos.sh` do `platform` recebe os três diretórios (README do `platform`, seção "Contrato com os serviços").

### Troca de senhas e chaves

A mecânica é a de [Troca de senha do banco](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform#troca-de-senha-do-banco), no README do `platform`; abaixo estão as credenciais do Billing, uma por procedimento. Os comandos usam o contexto do kind (no k3s, troque o `--context`). A senha nova tem só letras e dígitos (a do `openssl rand -hex 24` serve), porque entra crua na `MONGODB_URI`, e não passa por argumento, que ficaria no `ps` e na URL do pedido ao apiserver: o `jq` a recebe por variável de ambiente, e o `mongosh` a lê da variável `SENHA_NOVA` (e a do root, de `SENHA_ROOT`), que o `sh` do contêiner preenche com as duas primeiras linhas da entrada do `kubectl exec -i`. O Secret é gravado antes do banco, para o valor não ficar só numa variável do shell. Os pods leem o Secret só no start: até o restart, conexão nova com a senha antiga é recusada.

#### Senhas do MongoDB: `billing`, `exporter` e `root`

Os três usuários seguem os mesmos três passos. Escolha a linha da tabela e defina as variáveis que o passo 1 e o passo 2 usam:

| Credencial | Variáveis | O que reiniciar no passo 3 |
|---|---|---|
| `billing` | `chave=MONGO_BILLING_PASSWORD usuario=billing banco=billing` | os Deployments |
| `exporter` | `chave=MONGO_EXPORTER_PASSWORD usuario=exporter banco=admin` | o StatefulSet do banco, porque o exporter é sidecar dele |
| `root` | `chave=MONGO_INITDB_ROOT_PASSWORD usuario=root banco=admin` | o StatefulSet do banco, que lê a senha do root só no start (até lá, a variável de ambiente do contêiner guarda a antiga); o Job a relê no deploy seguinte |

`banco` é aquele em que o `replica-set.js` criou o usuário (`billing` em `billing`, os outros em `admin`): a troca no banco errado responde `User billing@admin not found`.

1. Leia a senha do root que o banco tem agora, a do Secret, e só então gere a senha nova e grave-a:

   ```bash
   senha_root=$(kubectl --context kind-pytstop-p4 -n pytstop-billing get secret billing-mongo -o json \
     | jq -r '.data.MONGO_INITDB_ROOT_PASSWORD | @base64d')
   senha=$(openssl rand -hex 24)
   kubectl --context kind-pytstop-p4 -n pytstop-billing get secret billing-mongo -o json \
     | VALOR="$senha" jq --arg chave "$chave" '.data[$chave] = (env.VALOR | @base64)' \
     | kubectl --context kind-pytstop-p4 replace -f -
   ```

2. Aplique no banco, como root. O root autentica com `$senha_root`, a do Secret lida no passo 1, e não com a variável de ambiente do contêiner: ela só acompanha o Secret no restart, e se o restart de uma troca anterior do `root` foi pulado, guarda uma senha que o banco já não aceita.

   ```bash
   { printf '%s\n%s\n' "$senha" "$senha_root"; cat <<JS
   db.getSiblingDB("admin").auth(process.env.MONGO_INITDB_ROOT_USERNAME, process.env.SENHA_ROOT)
   db.getSiblingDB("$banco").changeUserPassword("$usuario", process.env.SENHA_NOVA)
   print("senha de $usuario trocada")
   JS
   } | kubectl --context kind-pytstop-p4 -n pytstop-billing exec -i billing-mongo-0 -c mongo -- \
       sh -c 'read -r SENHA_NOVA && read -r SENHA_ROOT && [ -n "$SENHA_NOVA" ] && [ -n "$SENHA_ROOT" ] && export SENHA_NOVA SENHA_ROOT && exec mongosh --quiet --norc --file /dev/stdin'
   ```

   O `mongosh` lê o script como arquivo, e não da entrada interativa, que sai com 0 mesmo quando a autenticação falha. Deu certo quando o comando sai com 0 e imprime `senha de <usuario> trocada`; se a autenticação ou a troca falhar, ele imprime o erro do MongoDB (`MongoServerError: Authentication failed.`, por exemplo) e sai com código diferente de zero. Se o passo falhar, repita-o na mesma sessão, com a mesma `$senha` e a mesma `$senha_root`: o Secret já tem a `$senha`, e reiniciar os pods com o Secret diferente do banco os deixa sem acesso. Na troca do `root`, `$senha_root` é a única cópia da senha anterior depois do passo 1, então não feche a sessão antes de o passo 2 dar certo.
3. Reinicie quem usa a credencial: os Deployments com `kubectl --context kind-pytstop-p4 -n pytstop-billing rollout restart deployment`, e o banco com `kubectl --context kind-pytstop-p4 -n pytstop-billing rollout restart statefulset/billing-mongo`.

#### Keyfile

1. Grave um valor novo na chave `MONGO_KEYFILE` do Secret:

   ```bash
   valor=$(openssl rand -base64 756 | tr -d '\n')
   kubectl --context kind-pytstop-p4 -n pytstop-billing get secret billing-mongo -o json \
     | VALOR="$valor" jq --arg chave MONGO_KEYFILE '.data[$chave] = (env.VALOR | @base64)' \
     | kubectl --context kind-pytstop-p4 replace -f -
   ```

2. Reinicie o banco e espere: `kubectl --context kind-pytstop-p4 -n pytstop-billing rollout restart statefulset/billing-mongo` e, em seguida, `rollout status statefulset/billing-mongo`. O replica set tem um membro só, então nenhum outro precisa da chave antiga, mas o banco fica fora durante o restart.

#### Chave do link de decisão (`ORCAMENTO_LINK_SECRET`)

1. Apague o Secret: `kubectl --context kind-pytstop-p4 -n pytstop-billing delete secret billing-link`.
2. Rode o `make deploy` do `platform`, que o recria com um valor novo: `make -C ../postech-sw-arch-p4-platform deploy`.
3. Reinicie juntos a API e o consumidor: `kubectl --context kind-pytstop-p4 -n pytstop-billing rollout restart deployment/billing-service-api deployment/billing-service-consumidor`. Os links e checkouts já enviados deixam de valer: o orçamento que aguarda decisão fica sem link até vencer, e o atendente ainda decide por ele. Até o fim do restart, um link pode abrir numa réplica e dar 404 em outra.
