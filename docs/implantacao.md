# Implantação

> [↑ README](../README.md#como-rodar)

Manifests do Kubernetes em [`k8s/`](../k8s): o `base` e um overlay por ambiente, validados no job `build` do CI por `make manifests` (kubeconform com os schemas do Kubernetes 1.35, a versão do nó do kind, e `Secret` reprovado; trivy config sem achado HIGH ou CRITICAL, com a única exceção e o motivo em [`k8s/trivy-ignore.rego`](../k8s/trivy-ignore.rego); e uma conferência de que todo campo de senha do `replica-set.js` vem de `segredo(...)`, que a exceção do trivy não enxerga). O gitleaks varre o histórico do commit testado, com regras próprias para URI com senha (MongoDB e AMQP) e o teste que as faz reprovar um manifesto com a senha gerada ([`scripts/ci/testa-gitleaks.sh`](../scripts/ci/testa-gitleaks.sh)). Namespace `pytstop-billing`, prefixo `/billing` no Kong (ADR-038, ADR-042).

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

- **Ordem de subida**: o `implantar-servicos.sh` do `platform` apaga o Job anterior (Job é imutável), aplica o overlay com `apply --server-side` e espera, nesta ordem, o MongoDB, o Job completar e cada Deployment.
- **Job de inicialização**: roda em três etapas. O initContainer `replica-set` (`mongosh`) espera o `mongod`, inicia o replica set e cria os usuários `billing` e `exporter`, como root. O `banco` cria índices e validadores e marca a versão (`python -m src.banco`, com o usuário `billing`). O `seed` cadastra os preços de demonstração que faltam.
- **`aguarda-banco` e rollback**: os Deployments ficam no initContainer `aguarda-banco` (`python -m src.banco aguardar`) até a versão deles estar marcada: no rolling update, os pods novos esperam o Job da versão nova enquanto os antigos seguem servindo. Banco à frente da imagem não é espera: depois de um `kubectl rollout undo`, o pod revertido sobe sobre a versão que o Job novo marcou, e o rollback não desce a versão do banco (mudanças aditivas).
- **Configuração**: o ConfigMap `billing-service` (com o hash do conteúdo no nome: mudar um valor reinicia os pods) entra por `envFrom`, com `ENVIRONMENT=production` também no kind, para as guardas de boot valerem com os segredos gerados.
- **Segredos**: nenhum está nos manifests. O `make deploy` do `platform` os gera ([Segredos gerados](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform#segredos-gerados)), e eles entram por `secretKeyRef`; a `MONGODB_URI` se monta no pod por expansão de variável (`mongodb://billing:$(MONGO_BILLING_PASSWORD)@billing-mongo-0.billing-mongo...`), e o `kubectl describe` mostra o molde, não a senha. O molde está escrito uma vez, no container `api` de `api.yaml`, e o `replacements` de `k8s/base/kustomization.yaml` o copia para os outros processos e o Job (`make manifests` reprova se alguma cópia ficar com o valor sentinela).

  | Secret | Chaves | Quem lê |
  |---|---|---|
  | `billing-mongo` | `MONGO_INITDB_ROOT_PASSWORD`, `MONGO_BILLING_PASSWORD`, `MONGO_EXPORTER_PASSWORD`, `MONGO_KEYFILE` | o `mongod` (o root no primeiro boot do volume e o keyfile), o Job (root e `billing`), os processos (`billing`) e o exporter (`exporter`) |
  | `billing-link` | `ORCAMENTO_LINK_SECRET` | API e consumidor |
  | `rabbitmq` | `RABBITMQ_URL`, com o usuário `billing` | relay e consumidor |
  | `billing-mercadopago` (opcional) | `MP_ACCESS_TOKEN`, `MP_WEBHOOK_SECRET` | API, consumidor e `prazos`, com `MP_MODE=mercadopago` (no k3s, quando o Secret existe) |

- **Sondas da API**: liveness em `/api/v1/saude` e readiness em `/api/v1/saude/pronto` (MongoDB e versão do banco). A startup confere a mesma `/api/v1/saude` a cada 2 s, por até 60 s, e a liveness só vale depois dela: o primeiro 200 leva cerca de 10 s a 0,1 CPU, o request, e mais num runner disputado. O `terminationGracePeriodSeconds` é de 30 s.
- **Sondas dos processos**: relay e consumidor usam o arquivo de vida em `/tmp`: liveness com ele tocado há menos de 90 s e readiness com o conteúdo `pronto`. A startup (até 60 s) confere a idade, e não o `pronto`, para o broker fora no boot deixar o pod fora da prontidão em vez de reiniciá-lo. No `prazos`, a startup e a liveness conferem que o arquivo tem menos de 120 s (quatro ciclos do padrão), e a readiness, só que ele existe; como o `/tmp` sobrevive ao restart do contêiner, a startup só barra o arquivo com mais de 120 s (o restart pela liveness), e depois de um crash ou OOM com o arquivo mais novo o pod volta a `Ready` até o processo novo concluir o primeiro ciclo. As sondas exec rodam `python -c`, sem shell, e o `terminationGracePeriodSeconds` é de 45 s, com margem sobre a consulta de DNS que o SIGTERM não interrompe.
- **Sonda do MongoDB**: um `ping` pelo `mongosh`, que leva cerca de 1 s de CPU para subir: a liveness roda a cada 20 s, a readiness a cada 10 s, e as duas aceitam 10 s.
- **Usuários do MongoDB**: `root` (só no primeiro boot do volume e no Job), `billing` (`readWrite` e `dbAdmin` em `billing`, nos processos e no Job; o `dbAdmin` serve ao `collMod` dos validadores a cada deploy) e `exporter` (só `clusterMonitor`, que não lê as coleções de `billing`, mas inclui o oplog em `local`, onde ficam as escritas de `billing`: risco aceito de demonstração).
- **Imagens de terceiros**: `mongo:7.0.43` e `percona/mongodb_exporter:0.53.0`, por tag fixa e as da tabela de versões do `platform`. O job `trivy` do `security.yml` lista no summary de cada PR e da execução semanal os HIGH e CRITICAL com correção disponível das duas, sem reprovar o job. Risco aceito: na `mongo` quase todos estão em binários Go que o pod não executa (`gosu`, `mongodump` e as outras ferramentas da imagem), mais o OpenSSL e o `mongosh`; o exporter executa o binário dele, mas só o Prometheus alcança a porta 9216 (NetworkPolicy) e ele só fala com o `mongod` do próprio pod. A correção depende de um build novo do fornecedor, então a política é subir a versão a cada release do serviço, junto da tabela do `platform`.
- **Métricas**: todo pod de processo é anotado para o Prometheus da plataforma (`prometheus.io/scrape`, `port` e `path`): a API na porta `http` (8000), relay, consumidor e `prazos` na `metrics` (9100) e o MongoDB pelo exporter (9216), com `--collector.dbstats` e `--collector.replicasetstatus`; o `mongodb_up` diz se o exporter entrou no banco. O exporter usa um pool de conexões só (`--mongodb.global-conn-pool`) e o `mongod` sobe com `--quiet`: sem isso cada scrape abria e autenticava uma conexão e cada sonda abria uma, e o log do banco passava de mil linhas em 10 minutos. O `--quiet` tira as linhas "Connection accepted" e "Connection ended" e os registros dos comandos `drop` e `dropIndexes` (`CMD: drop` e `CMD: dropIndexes`); o `drop` segue visível em "Finishing collection drop" e "dropDatabase", com o namespace. A origem da conexão ("client metadata"), a autenticação que deu certo ou falhou, a query lenta, a falha de autorização, o aviso e o erro seguem no log. Sobra uma linha "Checking authorization failed" por scrape (uma a cada 15 s, cerca de 5.760 por dia): é o próprio exporter, com o papel `clusterMonitor`, perguntando em `admin.system.version` se o nó é de shard (`shardIdentity`), e não sinal de falha de autorização real; a real é a que não traz `shardIdentity` no comando.
- **Rede e endurecimento**: a NetworkPolicy só trata a entrada. Tudo é negado, e ficam liberados o Kong e o Prometheus na API, a API da Execução na API (validação de preços), o Prometheus nas métricas dos processos e do exporter e os pods do próprio namespace no MongoDB. Todo pod roda sem root (1001; 999 no MongoDB; 65534 no exporter), sem escalada de privilégio, sem capability, com seccomp `RuntimeDefault`, sem token de ServiceAccount e com a raiz somente leitura (`/tmp` num `emptyDir`).
- **Recursos** (request e limit): API 100m e 500m de CPU, 256 e 512 Mi de memória; relay, consumidor e `prazos` 50m e 250m, 128 e 256 Mi; MongoDB 100m e 500m, 256 Mi e 1 Gi (cache do WiredTiger em 0,25 GB); exporter 10m e 100m, 32 e 64 Mi; Job 50m e 500m, 128 e 256 Mi.
- **Borda**: no kind, o Kong publica o serviço em `https://localhost/billing`, com o certificado padrão dele (`curl -k`): `/billing/api/v1/*`, o Swagger em `/billing/docs` (a API sobe com `ROOT_PATH=/billing`), o link de decisão, o webhook e o simulador, os três com limite próprio; `/billing/metrics` e `/billing/api/v1/admin/*` respondem 404 sem chegar ao serviço.

Veja também: o [CD automatizado](cicd.md), o [kind local](como-rodar.md#no-kind-local) e a [troca de senhas e chaves](troca-de-senhas-e-chaves.md).
