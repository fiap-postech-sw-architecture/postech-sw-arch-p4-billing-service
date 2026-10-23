# Mensageria

> [↑ README](../README.md#mensageria)

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

Fatos que não vêm de comando: `OrcamentoAprovado`/`OrcamentoRecusado` (pelo link ou pelo atendente, com `decidido_por` = `sub` do atendente), `OrcamentoExpirado`, `PagamentoConfirmado`, `PagamentoRecusado`, `PagamentoExpirado` e o `PagamentoEstornado` do estorno automático. O `causation_id` deles é o comando que abriu o fluxo (`GerarOrcamento` para os do orçamento, `SolicitarPagamento` para os do pagamento), guardado no documento em `aberto_por` com o contexto de trace, e o evento sai como filho desse contexto, com um *span link* para o trace de quem retomou o passo: a saga segue num trace só (ADR-043). O processo `prazos` abre um span por registro que expira ou concilia, e a requisição HTTP (decisão pelo link ou pelo atendente, webhook, checkout do simulador) entra no link: a API instrumenta o FastAPI (ver [Observabilidade](observabilidade.md)).

O estorno automático (aprovação tardia, de valor ou moeda diferentes, ou segunda tentativa paga do mesmo checkout) publica `PagamentoEstornado{motivo=pagamento_apos_encerramento}` com o pagamento em qualquer estado: `RECUSADO`, `EXPIRADO` e `CANCELADO` passam a `ESTORNADO`, enquanto `SOLICITADO`, `CONFIRMADO` e `ESTORNADO` ficam como estão. Esse fato leva no `causation_id` o id do `SolicitarPagamento`. O mesmo `motivo` aparece na resposta ao `EstornarPagamento` que chega depois do estorno automático, mas com o id do comando no `causation_id`. A saga reconhece a resposta ao `EstornarPagamento` pelo `causation_id`, de qualquer `motivo`; o estorno automático só atualiza o resumo do pagamento na OS (ADR-040, passo 7; RFC-004, seção 5.2).

## RabbitMQ, outbox e consumidor

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
- **Rastreamento (ADR-043):** o relay publica num span de produtor (`PRODUCER`) filho do contexto gravado na outbox, com *span link* para quem retomou o passo quando a linha o traz, e injeta o seu no header. O consumidor abre o span de consumidor (`CONSUMER`) filho da publicação, e a outbox gravada nele leva esse contexto adiante. Os logs JSON levam `correlation_id`, `trace_id` e `span_id`. Laço ocioso não abre span: o poll do relay e a consulta do coletor de métricas são spans de banco sem pai, que o amostrador descarta (ver [Observabilidade](observabilidade.md)).
- **Métricas** (porta `METRICS_PORT` de cada processo): `pytstop_mensagens_publicadas_total{tipo}` no relay, `pytstop_mensagens_consumidas_total{tipo,resultado}` (`processada`, `duplicada`, `ignorada`, `retry`, `dlq`) no consumidor, `pytstop_reconexoes_ao_broker_total{componente}` nos dois e, de relay e API, `outbox_pendentes`, `outbox_dead` (os nomes do p3) e `pytstop_outbox_mais_antiga_segundos`. Tabela completa em [Observabilidade](observabilidade.md#métricas).
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

## Eventos

Os 13 eventos do catálogo do Billing (RFC-004, seção 5.3), no envelope da seção 5.2 (`id`, `tipo`, `versao`, `origem`, `correlation_id` = ordem de serviço, `causation_id`, `ocorrido_em`, `dados`): `OrcamentoGerado`, `GeracaoDeOrcamentoFalhou`, `OrcamentoAprovado`, `OrcamentoRecusado`, `OrcamentoExpirado`, `OrcamentoCancelado`, `PagamentoSolicitado`, `PagamentoConfirmado`, `PagamentoRecusado`, `PagamentoExpirado`, `PagamentoCancelado`, `PagamentoEstornado` e `EstornoDePagamentoFalhou`. Todo evento é validado contra os JSON Schemas da plataforma (cópia em `contratos/`) ao entrar na outbox, e os testes conferem um exemplo de cada. `link_decisao` e `checkout_url` carregam token e nunca vão para log.
