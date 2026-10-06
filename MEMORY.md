# Project Memory -- postech-sw-arch-p4-billing-service

<!-- last-consolidated: 2026-10-06 -->

Add-only log of project-specific learnings. New entries go to the top of each section. Never edit historical entries -- add a contradicting entry above instead.

Updated by AI agents at task end per `postech-ai-helper/ai/canonical/task-end-review.md`. The `last-consolidated` marker above is updated only when `/consolidate-memory` runs, not on every append.

## Recent decisions

- 2026-10-06 - Mercado Pago Checkout Pro: tentativa recusada ou cancelada nao encerra a cobranca (o comprador tenta de novo); so a expiracao fecha sem pagamento, e a preferencia leva `date_of_expiration` para pix/boleto. Aprovacao com valor divergente ou em cobranca ja encerrada e estornada na hora (chave `estorno-automatico-<id no provedor>`). O agregado decide (`Pagamento.aplicar_aprovacao`), o caso de uso estorna fora da transacao
- 2026-10-06 - Contrato de compensacao no Billing (levar para a RFC-004): `EstornarPagamento` sobre cobranca PENDENTE encerra a cobranca (PENDENTE -> ESTORNADO, nada a devolver) e responde `PagamentoEstornado`; sobre RECUSADO/EXPIRADO responde `EstornoDePagamentoFalhou`; estorno `in_process` no provedor e erro transitorio (repetir com a mesma chave). `CancelarOrcamento` sobre orcamento recusado/expirado e no-op sem evento
- 2026-10-06 - Sem `ENVIRONMENT` o servico assume producao (falha fechada: exige segredo do link proprio e enderecos explicitos); dev, compose e testes declaram `development`/`test`. Erro 422 de schema sai no mesmo envelope `erro` (com `detalhes`), diferente do p3
- 2026-10-06 - UnitOfWork do MongoDB e `executar(trabalho)` sobre `ClientSession.with_transaction` (read concern snapshot, write concern majority): `WriteConflict` reexecuta o trabalho relendo o agregado, entao decisao e expiracao concorrentes nunca gravam os dois; sem campo de versao no agregado. Chamada ao Mercado Pago fica fora da transacao (o trabalho pode rodar mais de uma vez)
- 2026-10-06 - Evento e um `IntegrationEvent` unico cujos campos sao o `dados` do catalogo da RFC-004 (tipo = nome da classe sem `Event`); o outbox guarda `_id` UUIDv7 (ordem de publicacao do relay) e o `envelope` pronto. Repetir comando ja concluido (gerar orcamento, solicitar pagamento, cancelar, estornar) devolve o estado sem reemitir evento (a entrega do primeiro ja e garantida pelo outbox); comando que falha responde a falha de novo a cada repeticao
- 2026-10-06 - Contextos `precos`, `orcamento` e `pagamento` independentes no import-linter: orcamento le precos e pagamento le orcamento por portas definidas no consumidor, com adapter na infraestrutura (ACL do p3). Base `compartilhado` copiada do p3 @ 08dcffe sem SQLAlchemy
- 2026-10-06 - Processo `prazos` (entrypoint `prazos`) expira orcamentos e pagamentos vencidos em ciclos; a API roda o seed idempotente de precos no boot com `RUN_SEED_ON_STARTUP=true` (compose)
- 2026-10-06 - Repo criado na fase 4 com branch protection na `main` desde o commit inicial (PR obrigatorio, admins incluidos, historico linear, conversas resolvidas, squash only). Motivo: a fase 3 perdeu ponto por commits diretos na main (29 no app, 11 na lambda) - spec `postech-sw-arch-p4/docs/superpowers/specs/2026-10-06-fase-4-bootstrap-design.md`

## Discovered conventions

- 2026-10-06 - Integracao usa um banco por sessao esvaziado a cada teste (fixture `banco` em tests/integracao/conftest.py); `uv run pytest --cov` roda unitarios + integracao, e o tests/conftest.py aponta o DOCKER_HOST do colima sozinho
- 2026-10-06 - Datas saem de `agora_utc()` truncadas em milissegundos (precisao do BSON date) e `valido_ate` em segundo cheio (o token do link assina a expiracao em epoch de segundos): ida e volta no MongoDB compara igual
- 2026-10-06 - Camada de aplicacao loga com `logging` da stdlib e `extra=` (o import-linter proibe structlog la); o `ExtraAdder` no pipeline do logging vira os extras em campos JSON antes do scrub de PII
## Gotchas

- 2026-10-06 - `exclude_also` do coverage com `\.\.\.` sem ancora casa `Callable[..., X]` na linha do `def` e exclui a funcao inteira do gate (escondeu `exigir_papel`); usar `^\s*\.\.\.\s*$`
- 2026-10-06 - `uv sync` instala o proprio projeto em modo editavel: na imagem o codigo vinha da arvore `src/` sem bytecode. O Dockerfile usa `--no-editable` e copia so o venv para o runtime
- 2026-10-06 - mongod aborta com `Too many open files` (WiredTiger panic, exit 133) se a suite cria e apaga um banco por teste: arquivo de colecao apagada fecha tarde. Corrigido com banco unico por sessao; o compose sobe o mongo com `nofile` 64000
- 2026-10-06 - Cliente PyMongo criado antes do `replSetInitiate` fica sem sessao (`Sessions are not supported by this MongoDB deployment`) ate o proximo heartbeat: criar o cliente depois de o primario ser eleito
- 2026-10-06 - `StrEnum` tambem e `str`: serializador que testa `str` antes de `Enum` deixa o membro do enum no payload. Testar `Enum` primeiro
- 2026-10-06 - Starlette 1.7: o TestClient emite deprecacao com `httpx`; `httpx2` no grupo dev resolve, e o respx segue mockando o `httpx` do adapter do Mercado Pago
- 2026-10-06 - gitleaks `generic-api-key` acusa codigo como `token = link.token(...)` e `por_link(token, aprovar=...)`: renomear a variavel em vez de abrir allowlist
- 2026-10-06 - PyJWT 2.13.x acumulou 27 advisories em out/2026: comecar em `pyjwt>=2.15.1` e `anyio>=4.15.1`. PyJWT 2.15 exige base64url valido na assinatura mesmo com `verify_signature=False` (JWT falso de teste precisa de segmento valido)

## Tech debt / TODO

- 2026-10-06 - LOW - Estorno automatico recusado pelo provedor so gera o log `estorno_automatico_recusado` (sem metrica `pytstop_` nem alerta dedicado); entra com a observabilidade
- 2026-10-06 - LOW - `causation_id` vai nulo no envelope ate o consumidor de comandos (PR da mensageria) repassar o id do comando
- 2026-10-06 - LOW - `/api/v1/saude` nao consulta o MongoDB (heranca do p3); readiness com dependencia fica para o PR dos manifests k8s
## Review lessons
