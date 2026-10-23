# API

> [↑ README](../../README.md#api)

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

Erros no envelope do p3, `{"erro": {"codigo", "mensagem", "id_requisicao"}}`, inclusive o 500 (com os cabeçalhos de segurança e o `X-Request-ID`): 401 `NAO_AUTENTICADO` (mensagem única), 403 `ACESSO_NEGADO`, 404 `ENTIDADE_NAO_ENCONTRADA` (só as duas rotas públicas com token têm código próprio, `LINK_DECISAO_INVALIDO` e `CHECKOUT_NAO_ENCONTRADO`, o mesmo 404 para qualquer falha do token), 409 para regra de negócio, 422 `VALOR_INVALIDO` para invariante de domínio e 503 para dependência fora. Falha de conexão ou timeout do MongoDB responde `DEPENDENCIA_INDISPONIVEL` com `Dependencia externa indisponivel; tente novamente`. Na API, o cliente e cada transação têm limite de 2 s; os workers mantêm operação/transação de 10 s e seleção de servidor de 5 s. Erros de programação e documento inválido continuam 500. A exceção, também herdada do p3 e igual no OS e na Execução, é o 422 de validação de schema: `{"detail": [{"type", "loc", "msg"}], "id_requisicao"}`, sem ecoar o valor recebido. A API não redireciona barra final, não anuncia o servidor e escreve um access log estruturado (`http_request`: método, caminho, status, duração e `request_id`, com o token do link de decisão mascarado) no lugar da linha de texto do uvicorn.

O OpenAPI exportado ([`docs/api/openapi.json`](openapi.json)) e a coleção Postman v2.1 ([`docs/api/billing.postman_collection.json`](billing.postman_collection.json)) saem do próprio app pelo `make docs-api`, que não sobe servidor nem banco. Um `curl` por rota está em [Exemplos da API](exemplos.md).

- **OpenAPI.** É o do `MP_MODE=simulado`: com o Mercado Pago real as três rotas do simulador não existem.
- **Coleção.** Uma requisição por rota, com corpo e resposta de exemplo e as variáveis `baseUrl`, `token`, `ordem_id`, `orcamento_id`, `pagamento_id`, `token_do_link`, `token_do_checkout`, `codigo` e `sku`. O Billing não tem login: o `token` é o `access_token` do OS Service, colado a mão, e o cadastro de serviço e de peça e a consulta do orçamento por ordem guardam `codigo`, `sku` e `orcamento_id` num script de teste. O resto vem de fora, e a descrição da coleção diz de onde.
- **Teste.** O `make test` reprova se os dois arquivos ficarem atrás do código, ou se a coleção perder o token bearer, o `noauth` das rotas sem JWT, o `Content-Type` dos corpos, o `{{baseUrl}}` das URLs, a resposta de exemplo ou os scripts de teste.
