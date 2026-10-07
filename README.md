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
| `PRAZOS_INTERVALO_SEGUNDOS`, `PRAZOS_HEARTBEAT`, `METRICS_PORT` | `30`, `/tmp/prazos-heartbeat`, `9100` | processo `prazos` (`METRICS_PORT`, a porta do `/metrics`, vale para os três processos sem API, como no OS e na Execução) |
| `RABBITMQ_URL` | sem padrão | broker com o usuário do serviço (`amqp://billing:<senha>@<host>:5672/%2F`), exigido pelo relay e pelo consumidor; o usuário vai no `user_id` de toda publicação |
| `RELAY_HEARTBEAT`, `CONSUMIDOR_HEARTBEAT` | `/tmp/relay-heartbeat`, `/tmp/consumidor-heartbeat` | arquivo de vida do relay e do consumidor |
| `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME` | `false`, `http://jaeger:4317`, `billing-service` | exportação dos traces do relay, do consumidor e do `prazos` pelo protocolo do OpenTelemetry (OTLP) sobre gRPC; desligada, o contexto de trace segue nas mensagens do mesmo jeito, nos headers `traceparent`/`tracestate` do padrão W3C Trace Context |
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
- **Retenção por índice TTL:** linhas entregues da outbox somem 7 dias depois (`entregue_em`, só com `status: entregue`) e `mensagens_processadas`, 30 dias depois. As linhas `dead` (alerta pela métrica `outbox_dead`) ficam 30 dias (`morta_em`) para investigação; corrigida a causa, `db.outbox.updateOne({_id: UUID("<id>")}, {$set: {status: "pendente", tentativas: 0, proxima_tentativa_em: new Date()}, $unset: {morta_em: "", ultimo_erro: ""}})` no `mongosh` devolve a linha ao relay.
- **Rastreamento (ADR-043):** o relay publica num span de produtor (`PRODUCER`) filho do contexto gravado na outbox, com *span link* para quem retomou o passo quando a linha o traz, e injeta o seu no header. O consumidor abre o span de consumidor (`CONSUMER`) filho da publicação, e a outbox gravada nele leva esse contexto adiante. Os logs JSON levam `correlation_id`, `trace_id` e `span_id`. Laço ocioso não abre span.
- **Métricas** (porta `METRICS_PORT` de cada processo): `pytstop_mensagens_publicadas_total{tipo}`, `pytstop_mensagens_consumidas_total{tipo,resultado}` (`processada`, `duplicada`, `ignorada`, `retry`, `dlq`) e, no relay, `outbox_pendentes` e `outbox_dead`, os nomes do p3.
- **Saúde:**
  - Relay e consumidor tocam um arquivo a cada volta do laço (`RELAY_HEARTBEAT`, `CONSUMIDOR_HEARTBEAT`), com `pronto` só enquanto a conexão com o broker está de pé; fora dela, inclusive na espera antes de reconectar, o arquivo diz `conectando`. A liveness confere a idade do arquivo, e a readiness, também o conteúdo.
  - É um arquivo só, e não o par heartbeat e pronto do OS e da Execução, porque a mesma sonda lê os dois sinais de uma vez. O `prazos`, sem conexão para provar, só toca o arquivo dele.
  - A reconexão espera de 1 a 30 s, dobrando, com jitter: entre a metade e o total do atraso, para as réplicas que perderam o broker juntas não voltarem juntas. Vale também para a conexão que cai logo depois de aberta, para o consumidor cancelado pelo broker e para o nome do broker que deixa de resolver no DNS (o Service headless do RabbitMQ some do DNS sem pod pronto, no boot ou depois de uma queda): o processo fica fora da prontidão, com o log `broker_unavailable`, em vez de cair e reiniciar. Na abertura da conexão, contam como broker fora o erro de conexão do pika (`AMQPError`), o nome sem resolução (`socket.gaierror`) e o broker que aceita a conexão TCP e não responde o AMQP no prazo da pilha do pika (15 s, `AMQPConnectorStackTimeout`); qualquer outro erro derruba o processo, para o orquestrador reiniciá-lo e o defeito aparecer: falta de descritores, certificado TLS recusado e erro de disco ao tocar o arquivo de vida.
  - Com o DNS mudo (sem resposta, nem a de nome inexistente), a consulta do pika não tem prazo e prende a tentativa pelo tempo do resolver: 20 s medidos com a glibc e o `resolv.conf` padrão do Kubernetes (`ndots:5`, três domínios de busca), que o `timeout`, o `attempts` e a lista de domínios multiplicam. O arquivo de vida é tocado de novo quando a tentativa falha, então a idade dele chega, no máximo, ao maior entre esse tempo e a espera do backoff (30 s), e não à soma dos dois. O SIGTERM só é atendido quando a consulta volta (medido: sinal aos 3 s, laço encerrado aos 20 s), sem conexão nem mensagem em curso para perder. A sonda de liveness (60 s no compose) e o `terminationGracePeriodSeconds` do manifesto do Kubernetes precisam de margem sobre o tempo do resolver: o padrão de 30 s cobre os 20 s medidos, e o encerramento do Docker (10 s) interrompe o processo à força.
  - Com alarme de memória ou disco no broker, a conexão que publica fica bloqueada. O relay não reivindica linha nova enquanto ela estiver bloqueada, e o pika a derruba em 8 s, mesmo ociosa (abaixo do prazo de encerramento do Docker e do Kubernetes): o publish preso cai junto, com a linha devolvida sem gastar tentativa, e o relay reconecta com backoff.
- **Encerramento:** SIGTERM termina a mensagem em curso, cancela o consumo (as mensagens pré-buscadas voltam à fila) e fecha as conexões. API, `prazos` e consumidor não sobem sem os schemas dos contratos.
- **Contratos:** `contratos/` é cópia do `platform` no commit gravado em `contratos/ORIGEM`: os schemas e exemplos das mensagens do Billing, o `asyncapi.yaml` e, em `contratos/rabbitmq/`, a topologia do broker (definitions, permissões, configuração, plugins e o script que cria os usuários) que o compose e os testes sobem. Um teste baixa o `platform` nesse commit e compara os arquivos byte a byte (marcador `rede`: sem acesso ao GitHub, falha no CI e é pulado fora dele); outro valida os exemplos do `platform`.

Como rodar:

```bash
make compose-up          # relay e consumidor sobem com o resto (RabbitMQ na 5672, console em 15672, usuario admin)
docker compose ps relay consumidor   # saudaveis = conectados ao broker
# fora do compose, com o MongoDB e o RabbitMQ do compose no ar (uv run nao le o .env sozinho)
cp .env.example .env
uv run --env-file .env python -m src.relay
METRICS_PORT=9101 uv run --env-file .env python -m src.consumidor   # outro terminal, outra porta de metricas
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

- **Unitários:** domínio, matrizes de transição dos agregados, link assinado, contrato do adapter do Mercado Pago com respx, circuit breaker, JWKS com chave RSA gerada no teste, contrato das mensagens, configuração e OpenTelemetry.
- **Contrato** (`tests/contratos`): a cópia confere com o `platform` no SHA de origem, que o teste baixa pelo GitHub, e os exemplos de lá validam.
- **Integração** via testcontainers, com MongoDB 7.0.43 real em replica set e RabbitMQ 4.3.6 com as definitions da plataforma:
  - API e casos de uso: matriz papel × rota gerada do OpenAPI, atomicidade da outbox, idempotência e corridas reais entre decisão, webhook e expiração.
  - Mensageria: relays concorrentes e lease retomado; consumidor com origem, contrato, entrada hostil, retry por nível e DLQ; os quatro comandos com a causa de cada resposta; e o fluxo de ponta a ponta do comando publicado ao evento em `pytstop.eventos`, com o trace encadeado, a mensagem venenosa, a mensagem sem rota, o alarme de memória e o broker parado.
- **Gate:** 90% de linhas e ramos (`.coveragerc`); o SonarQube aplica o quality gate (ADR-041). O resumo da cobertura por pacote sai de `make test` seguido de `uv run python scripts/cobertura_resumo.py coverage.xml`, e o CI publica o mesmo resumo no summary do job `test` de cada run ([Actions → CI](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service/actions/workflows/ci.yml)).
