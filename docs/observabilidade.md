# Observabilidade

> [↑ README](../README.md#observabilidade)

Traces e métricas seguem o [ADR-043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/043-observabilidade-distribuida.md). Os quatro processos (API, relay, consumidor e `prazos`) instalam o SDK do OpenTelemetry pela mesma função (`criar_provedor`) e exportam os traces por OTLP/gRPC para o Jaeger quando `OTEL_ENABLED=true`. As métricas são as do Prometheus, no `/metrics` que cada processo já serve (a API na porta 8000 e os outros três na 9100; nenhuma porta nova), com os rótulos que o Prometheus põe em todo alvo (`job`, `app`, `processo`, `namespace` e `pod`).

Os painéis, os alertas e o [catálogo das métricas dos três serviços](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/README.md#catálogo-de-métricas) ficam no `platform`, em `observabilidade/`; abaixo estão as séries, os spans e os logs deste serviço.

Dashboards relacionados no Grafana: [Orçamentos](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/dashboards/pytstop-negocio-orcamentos.json), [Pagamentos](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/dashboards/pytstop-negocio-pagamentos.json), [Saga](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/dashboards/pytstop-saga.json) (o painel "Último ciclo do prazos" lê a série do processo `prazos`), [Mensageria](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/dashboards/pytstop-mensageria.json), [Serviços](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/dashboards/pytstop-servicos.json), [Bancos](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/dashboards/pytstop-bancos.json). Consulte a documentação dos [painéis de negócio](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/negocio.md) e dos [técnicos](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/README.md).

O Jaeger do platform usa `MEMORY_MAX_TRACES=250`: o trace mais antigo sai quando entra o 251º, sem TTL; reiniciar o pod apaga todos. Capture o trace logo após gerar o fluxo ([retenção e limites](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/README.md#retenção-e-prazos)).

## Spans

| Processo | Span | Filhos |
|---|---|---|
| API | `SERVER` por requisição, nomeado pelo template (`POST /api/v1/publico/orcamentos/{token}/decisao`), filho do `traceparent` que chegou | `CLIENT` por comando ao MongoDB (`<banco>.<comando>`) e por chamada ao Mercado Pago (httpx) |
| relay | `publish <tipo>` (`PRODUCER`), filho do contexto gravado na outbox | `CLIENT` do MongoDB (a marcação da linha) |
| consumidor | `process <tipo>` (`CONSUMER`), filho da publicação | `CLIENT` do MongoDB e do Mercado Pago |
| `prazos` | `prazos` (`INTERNAL`), aberto na primeira escrita da transação que expira ou concilia um registro | `CLIENT` do MongoDB (as escritas e o commit) |

- O pika não tem instrumentação própria: os spans de publicação e de consumo são os acima, e o contexto atravessa a outbox e os headers AMQP pela mão do serviço (ver [Mensageria](mensageria.md)).
- O span de banco leva `db.system`, `db.name`, `db.mongodb.collection`, `net.peer.name`, `net.peer.port` e, em `db.statement`, só o nome do comando (`find`, `update`, `commitTransaction`).
- A decisão do cliente, o webhook e o checkout do simulador gravam o contexto da requisição na outbox, e o evento que a saga publica depois aponta para ela por *span link*.
- A API nasce no trace de quem a chamou (`traceparent`). Cada passo retomado por pessoa, por sistema externo ou por prazo fica num trace próprio, ligado ao da saga por esse link.
- No SIGTERM cada processo descarrega os spans que esperam no lote: relay, consumidor e `prazos` chamam o `shutdown` do provider ao sair, e a API o chama no fim do `lifespan`. No container, com o uvicorn como PID 1, os spans já chegavam (o `atexit` do SDK roda); fora dele, no host, o uvicorn re-levanta o sinal e o processo morre sem rodá-lo.
- O que pesa é o prazo de saída com o Jaeger fora e a fila cheia (2.048 spans): a API levava 30,4 s para sair em container, todo o `terminationGracePeriodSeconds`. Com `OTEL_EXPORTER_OTLP_TIMEOUT=2` leva 3,9 s (medido junto de `OTEL_BSP_EXPORT_TIMEOUT=2000`, que o SDK 1.45.1 não usa).

## Amostragem e o que não vira trace

- **Span de cliente sem pai é descartado** (`AmostradorDeRaiz` sob um `ParentBased`): o poll do relay, a consulta do coletor de métricas a cada raspagem e o ciclo ocioso do `prazos` são só consultas ao MongoDB. Cada uma abriria um trace de um span e encheria o Jaeger (buffer limitado a 250 traces). Dentro de um trace (requisição, mensagem, transação que escreve) o span de cliente segue o pai.
- **O `prazos` só abre o span quando a transação escreve.** Com o Mercado Pago real, cada ciclo consulta o provedor e reaplica a tentativa em andamento; só a primeira vez muda o registro. Sem escrita não há span, e as consultas, sem pai, caem no descarte acima: o ciclo ocioso não deixa trace, nem com o gateway de conciliação ligado. A consulta ao provedor em si não tem span.
- `/metrics` e as sondas (`/api/v1/saude` e `/api/v1/saude/pronto`) ficam fora da instrumentação HTTP.
- Os spans de cada `receive` e `send` do ASGI não são criados (o `SERVER` já leva o status), e o span nativo do FastAPI fica desligado, para não duplicar o `SERVER`.

## O que não é exportado, e por quê

| Fica de fora | Por quê | Como |
|---|---|---|
| Token do link de decisão, id do pagamento e token do checkout, no caminho e na query | São credenciais (ADR-039): quem tem o link decide o orçamento e quem tem o checkout paga a cobrança | O hook `trocar_url_pelo_template` grava o template da rota em `http.target` e `url.path`, `esquema://REDACTED<template>` em `http.url` e `url.full`, e `REDACTED` em `url.query`. Requisição que nenhuma rota casou, como o 404 do token com uma barra a mais, fica `nao_roteada`. Um `Host` malformado (`[`, `a]b`) não desfaz isso: o span sai só com o template |
| Id da preferência, id do pagamento e `external_reference`, na URL do `CLIENT` do httpx ao Mercado Pago | O id da preferência compõe o link do checkout (`pref_id`), o do pagamento é credencial (ADR-039) e a busca leva o id do pagamento na query | O `request_hook` do httpx (`trocar_url_do_cliente_pelo_template`) grava o template em `http.url` e `url.full`: `/checkout/preferences/{id}`, `/v1/payments/{id}`, `/v1/payments/search` (sem a query) e `/v1/payments/{id}/refunds`. Caminho que a lista não conhece sai `nao_roteada`; no simulador não há chamada |
| O documento do MongoDB | Os documentos levam o `link_decisao`, o `checkout_url` e o valor cobrado | `capture_statement` desligado |
| Cabeçalhos e corpo de requisições e respostas, inclusive `Authorization` e `x-signature` | Segredo e dado do pagador | A instrumentação só os captura se configurada, e não é |
| Texto de exceção e descrição do status de erro | Trazem o que o erro viu: o `Authorization` que o httpx recusou enviar, o CPF de uma requisição, o documento que o MongoDB rejeitou. O log passa pelo `scrub`; o span não passava | O processador do provider (o primeiro, antes do exportador) tira `exception.message`, `exception.stacktrace` e a descrição do status de todo span, do serviço ou da instrumentação. Ficam `exception.type` e `pytstop.falha`, só com o nome da classe |
| `Host` e endereço do servidor | O `Host` é texto do chamador, sem limite: `http.server_name` e a autoridade do `http.url` (usuário, senha, host e porta) o repetem. `http.host`, `server.address` e as portas do servidor são o endereço do próprio pod, não o do chamador, e também não servem à operação | O hook grava `REDACTED` nos textos e `0` nas portas, quando existem no span, e `REDACTED` no lugar da autoridade da URL, que mantém o esquema e o template da rota |
| `User-Agent` e endereço e porta do par da conexão | Texto livre sem limite e dado de rede de quem chamou, sem uso operacional; o Jaeger guarda 250 traces em memória, sem como apagar um titular | O hook grava `REDACTED` nos textos e `0` nas portas |
| `baggage` do chamador | O ADR-043 prevê só `traceparent` e `tracestate`; o padrão do SDK somaria o `baggage`, que a API aceitaria e o httpx repassaria ao Mercado Pago | O propagador global é só o `TraceContext` (`instalar_propagador`). O `tracestate` acima de 512 caracteres não vai para a outbox nem para o AMQP |

Uma ressalva. O httpx injeta o `traceparent` em toda chamada, inclusive nas ao Mercado Pago (só ids aleatórios do trace).

## Métricas

| Métrica | Tipo e rótulos | Processo | Quando conta |
|---|---|---|---|
| `http_request_duration_seconds` | histograma `method` (GET, POST, PUT, PATCH, DELETE, HEAD e OPTIONS; qualquer outro vira `OUTRO`), `rota` (template), `status` | API | fim de cada requisição |
| `pytstop_orcamentos_tempo_decisao_segundos` | histograma `decisao` (`aprovado`, `recusado`); 16 baldes, de 0,1 s a 3 dias | API | depois do commit da decisão, pelo link ou pelo atendente: da geração à decisão. O expirado não entra |
| `pytstop_pagamentos_valor_centavos_total` | contador `estado` (`confirmado`, `estornado`) | API (webhook e simulador), `prazos` (conciliação), consumidor (estorno) | o pagamento entra em `CONFIRMADO` ou o confirmado vai a `ESTORNADO`, em centavos inteiros. Na API e no `prazos`, depois do commit; no consumidor, pelo menos uma vez, como as demais contagens do pagamento. A aprovação tardia estornada direto não entra (está em `pytstop_pagamentos_estornados_total`) |
| `pytstop_pagamentos_estornados_total` | contador `motivo` (`compensacao`, `pagamento_apos_encerramento`) | API, `prazos`, consumidor | estorno concluído no provedor |
| `pytstop_estornos_automaticos_recusados_total`, `pytstop_cancelamentos_de_cobranca_recusados_total` | contadores | API, `prazos`, consumidor | o provedor recusou o estorno automático ou fechar o checkout |
| `pytstop_mercadopago_requisicoes_total` | contador `operacao` (5) e `resultado` (7) | API, `prazos`, consumidor | cada tentativa; as 35 combinações nascem com zero |
| `pytstop_circuit_breaker_aberto` | gauge `dependencia` (`jwks`, `mercadopago`) | `jwks` na API; `mercadopago` na API, no `prazos` e no consumidor, só com `MP_MODE=mercadopago` (o simulador não cria o disjuntor) | abre e fecha |
| `pytstop_webhook_assinatura_invalida_total`, `pytstop_jwks_falhas_total` | contadores | API | `x-signature` ausente ou inválida; busca ao JWKS que falhou |
| `pytstop_mensagens_publicadas_total` | contador `tipo` (os 13 eventos) | relay | confirmação do broker |
| `pytstop_mensagens_consumidas_total` | contador `tipo` (os 4 comandos e `desconhecido`) e `resultado` (5) | consumidor | mensagem resolvida |
| `outbox_pendentes`, `outbox_dead`, `pytstop_outbox_mais_antiga_segundos` | gauges calculados na raspagem | relay e API | pendentes e em entrega; as `dead`; a idade da linha pendente mais antiga (0 sem pendente) |
| `pytstop_reconexoes_ao_broker_total` | contador `componente` (`relay`, `consumidor`) | relay, consumidor | cada espera antes de reconectar ao RabbitMQ |
| `pytstop_prazos_ultimo_ciclo_timestamp_seconds` | gauge | `prazos` | fim de cada ciclo |

- **Nasce com zero:** toda série de rótulo fechado existe no `/metrics` desde o boot do processo que a emite, exceto o disjuntor do Mercado Pago, que só existe com o gateway real. O `increase()` do Prometheus perde o primeiro evento de uma série que só aparece com ele, e no volume de uma demonstração o painel mostraria zero. O relay pré-inicializa os eventos que publica, o consumidor os comandos que consome e os dois o próprio `componente`.
- **Baldes do tempo até a decisão:** 0,1; 0,5; 1; 2; 5; 15; 30; 60 e 300 s; 15 min; 1, 4 e 12 h; 1, 2 e 3 dias, porque o orçamento vale 72 h. Começam onde a demonstração mede, cerca de 1 s: o `histogram_quantile` interpola linearmente dentro do balde, e com o primeiro em 60 s qualquer decisão rápida saía com p90 de 54 s (0,9 × 60), inclusive a única de uma recusa. `tests/unitarios/test_metricas_orcamento.py` refaz essa conta com os baldes antigos e exige p90 de até 2 s para decisões de 1 s. Trocar os baldes não converte as séries antigas: uma janela do Prometheus que atravessa a troca soma dois conjuntos de `le`, e o percentil dela não vale.
- **O que o contador perde:** ele vive na memória do processo. O que o processo conta depois da última raspagem (a cada 15 s) e antes de o pod ser trocado não chega ao Prometheus, e o `increase()` não o vê. Se o consumidor é derrubado poucos segundos depois de um estorno, `pytstop_pagamentos_estornados_total` e o valor estornado ficam abaixo do que o OS contou ao receber o `PagamentoEstornado`; a lista exata está no banco do Billing. A série em zero no boot cobre o outro lado, o primeiro evento do pod, e vale nos três processos que contam estorno (`tests/unitarios/test_preinicializacao.py`).
- **Dinheiro em centavos inteiros:** o Prometheus guarda `float64`, então a métrica soma `int(valor * 100)` do `Decimal` e o painel divide por 100.
- **A raspagem da API e a do relay consultam o MongoDB:** duas contagens e a linha pendente mais antiga, que lê o índice `(status, criado_em)`. Com o relay parado a API segue exportando a idade, que mantém o alerta de outbox parada de pé. As três consultas dividem um prazo de 2 s (`PRAZO_DA_RASPAGEM_S`).
- **Banco fora ou travado:** o `/metrics` responde em cerca de 2 s sem essas três séries, que somem em vez de zerar, e o resto da raspagem segue. Cada raspagem que falha deixa um WARNING `outbox_metrics_unavailable` com a classe do erro do driver em `tipo_do_erro` (por exemplo `ExecutionTimeout` ou `NetworkTimeout`), nunca o texto dele. Sem o prazo eram 10,1 s, mais que o `scrape_timeout` de 10 s do Prometheus, o que deixaria o alvo da API em `up=0` (inferência a partir do tempo da resposta, não de uma raspagem observada). A consulta não vira trace (é um span de banco sem pai), e a API sobe sem esperar o MongoDB.
- O rótulo das reconexões é `componente` (`relay` ou `consumidor`). O Prometheus já põe `processo` em todo alvo, e um rótulo da métrica com o mesmo nome viraria `exported_processo`.

## Logs

Uma linha JSON por evento no stdout de cada processo, com o nome do evento em inglês (`http_request`, `command_ignored`, `mercadopago_request_rejected`, `audit_budget_decided_by_attendant`).

- **Campos.** O `trace_id` e o `span_id` do span corrente, o `correlation_id` (a ordem de serviço) e, na API, o `request_id` (o `X-Request-ID` que o Kong gera, devolvido na resposta).
- **Access log.** É o `http_request` do middleware, com método, caminho, status e duração; o do uvicorn fica desligado (`--no-access-log`).
- **Mascaramento.** Antes de sair, toda linha passa pelo mascaramento de dados pessoais ([`logging.py`](../src/compartilhado/infraestrutura/logging.py)). As chaves de nome sensível (`token`, `secret`, `senha`, `password`, `signature`, `authorization`, `api_key`, `checkout_url`, `link_decisao`, `telefone`, `celular`, `phone` e `contato`) viram `***`. CPF, CNPJ, e-mail, telefone, o token do link de decisão e do checkout e o token do Mercado Pago são mascarados dentro de qualquer texto.
- **Texto do provedor.** O texto livre da recusa do Mercado Pago não entra em nenhum log (ver Mercado Pago, em [Regras do serviço](regras-do-servico.md#mercado-pago)).
- **Onde ver.** No cluster, o Promtail do `platform` leva o stdout dos pods ao Loki, que o Grafana consulta.

## Como ver

```bash
make -C ../postech-sw-arch-p4-platform port-forward
```

Abre o Jaeger em `localhost:16686`, o Grafana em `localhost:3000` e o Prometheus em `localhost:9090` (repositório `platform`, irmão deste, com o kind no ar). Sem tráfego o Jaeger fica vazio: o E2E da saga gera os traces (`make -C ../postech-sw-arch-p4-platform e2e`).

No Jaeger, o serviço é `billing-service`. Filtre pelo nome do span (`process GerarOrcamento`, `publish OrcamentoGerado`, `prazos`, `POST /api/v1/publico/orcamentos/{token}/decisao`), pela tag `messaging.message.conversation_id` com o id da ordem de serviço ou pela tag de processo `pytstop.processo` (`api`, `relay`, `consumidor` ou `prazos`). Todo log JSON de um span tem o `trace_id` dele, para ir do log ao trace.

O kind liga `OTEL_ENABLED=true` (ConfigMap em `k8s/base/kustomization.yaml`); o compose deixa `false`. Sem cluster, as métricas aparecem direto na API do compose:

```bash
curl -s localhost:8002/metrics | grep '^pytstop_'
```
