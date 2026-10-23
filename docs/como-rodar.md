# Como rodar

> [↑ README](../README.md#como-rodar)

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

Variáveis (lista completa com valores de demonstração em [`.env.example`](../.env.example)):

| Variável | Padrão (development/test) | Para que |
|---|---|---|
| `ENVIRONMENT` | `production` se ausente | `development`/`test` liberam os padrões abaixo; outro valor recusa o boot |
| `MONGODB_URI`, `MONGODB_DB` | `mongodb://localhost:27017/?directConnection=true`, `billing` | banco (replica set) |
| `JWKS_URL` | `http://localhost:8000/.well-known/jwks.json` | chave pública para validar o JWT (emissor e audiência são fixos, ver Autenticação em [Segurança](../README.md#segurança)) |
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
| `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME` | `false`, `http://jaeger:4317`, `billing-service` | exportação dos traces da API, do relay, do consumidor e do `prazos` pelo protocolo do OpenTelemetry (OTLP) sobre gRPC; desligada, o contexto de trace segue nas mensagens do mesmo jeito, nos headers `traceparent`/`tracestate` do padrão W3C Trace Context |
| `OTEL_EXPORTER_OTLP_TIMEOUT`, `OTEL_BSP_EXPORT_TIMEOUT` | `2` (s), `2000` (ms) | `OTEL_EXPORTER_OTLP_TIMEOUT` é o prazo de cada envio de spans (o SDK usa 10) e é só ele que encurta a saída: com o Jaeger fora e a fila cheia, a API leva 3,9 s para sair (30,4 s com os padrões do SDK, todo o `terminationGracePeriodSeconds`). `OTEL_BSP_EXPORT_TIMEOUT` (padrão 30000) o SDK 1.45.1 guarda e não lê, então não muda nada. Vêm do ConfigMap e do compose |
| `CONTRATOS_DIR` | `contratos/` do repositório (`/app/contratos` na imagem) | schemas das mensagens |
| `RUN_SEED_ON_STARTUP` | `false` (o compose liga) | seed idempotente da tabela de preços no boot da API |
| `ROOT_PATH` | vazio | prefixo da borda (`/billing` no Kubernetes), passado ao `--root-path` do uvicorn: o Swagger de `/billing/docs` busca o `/billing/openapi.json`, e o OpenAPI leva o prefixo em `servers` (ADR-038) |

Processos da imagem (`entrypoint.sh`): `api` (padrão), `prazos`, `relay`, `consumidor`, `banco` (preparação idempotente do MongoDB, antes dos outros) e `aguarda-banco` (espera essa preparação; ver [Implantação](implantacao.md)). A imagem roda como o usuário 1001, sem shell de login, com o sistema de arquivos só leitura no compose e no Kubernetes.

## No kind local

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
