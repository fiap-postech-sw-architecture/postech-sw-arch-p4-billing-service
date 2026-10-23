# Exemplos da API

> [↑ README](../../README.md#api) · [↑ API](README.md)

Os `curl` das rotas do OpenAPI, um por rota e agrupados por fluxo. Os que levam token precisam do OS Service no ar (ele emite o JWT e publica o JWKS), então rodam contra a stack completa no kind, pela borda: o Kong do `platform`, com certificado autoassinado, daí o `-k`. No compose do serviço (`make compose-up`, `http://127.0.0.1:8002`) respondem só as sondas, o link público, o webhook e o simulador. O OpenAPI e a coleção Postman, com um exemplo de corpo e de resposta por rota, estão em [`docs/api/`](./), e os fluxos por trás de cada rota, em [`docs/arquitetura.md`](../arquitetura.md).

- **Variáveis.** Cada bloco usa as que os anteriores preenchem. `BASE_URL` e `TOKEN` saem da preparação; `ORDEM_ID` é o `id` de uma OS aberta no OS Service; o resto sai das respostas.
- **Quem pode o quê.** As escritas da tabela de preços são do `admin`; a leitura dela, o orçamento e o pagamento são do `atendente`, e a validação de itens, do `mecanico`. O `admin` passa em todas, então o token da preparação serve a todos os blocos.
- **Teste.** `tests/unitarios/test_documentacao.py` reprova este documento quando uma rota do OpenAPI fica sem `curl`, quando um `curl` do Billing não casa com nenhuma rota (caminho ou método trocado), quando a query dele não é a que a rota declara (sobra um nome, ou falta o obrigatório), quando um corpo sai do schema da rota e quando falta o `Authorization: Bearer` de uma rota protegida ou o `Content-Type` de um corpo (sem ele o FastAPI responde 422).

## Preparação

O token do admin pelo login do OS Service. O e-mail do admin é a chave `ADMIN_EMAIL` do ConfigMap do OS Service, achado pela chave e não pelo nome, como no [runbook da saga](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/operacao/runbook-saga.md) do `platform`; a senha fica no Secret `os-admin`, e o `printf`, embutido no shell, a entrega ao `curl` pela entrada padrão, fora da lista de processos:

```bash
export BASE_URL=https://localhost/billing
email=$(kubectl --context kind-pytstop-p4 -n pytstop-os get configmaps -o json | jq -r 'first(.items[].data.ADMIN_EMAIL // empty)')
senha=$(kubectl --context kind-pytstop-p4 -n pytstop-os get secret os-admin -o jsonpath='{.data.ADMIN_PASSWORD}' | base64 -d)
export TOKEN=$(printf '{"email": "%s", "senha": "%s"}' "$email" "$senha" | curl -sk https://localhost/os/api/v1/autenticacao/login -H 'Content-Type: application/json' --data @- | jq -r .access_token)
```

O token vale 15 minutos. O `ORDEM_ID` é o `id` da OS que o OS Service abriu (`POST /os/api/v1/ordens-de-servico`, no [README dele](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service#readme)); o valor do bloco é só um exemplo, e deve ser trocado pelo da sua OS. O orçamento só existe depois que o mecânico conclui o diagnóstico e a saga manda o `GerarOrcamento`:

```bash
export ORDEM_ID=3c9a7e10-5b2f-4f6d-8a41-0e2d9b7c6f55
```

## Saúde

Liveness (com o modo do pagamento) e readiness (503 com o MongoDB fora ou sem a versão do banco), sem token. O `/metrics` não está no OpenAPI e fica fora da borda ([Observabilidade](../observabilidade.md)):

```bash
curl -sk "$BASE_URL/api/v1/saude"
curl -sk "$BASE_URL/api/v1/saude/pronto"
```

## Tabela de preços

A escrita é do admin. O código e o SKU têm letras maiúsculas e dígitos separados por hífens (até 50 caracteres), o preço vai como texto, e o `DELETE` desativa sem apagar: o preço continua legível, com `ativo: false`, e os orçamentos já gerados não mudam. O seed já cadastra `SRV-TROCA-OLEO`, `PEC-OLEO-5W30` e os outros códigos de demonstração; os dois abaixo são novos:

```bash
export CODIGO=SRV-LIMPEZA-INJETORES
curl -sk -X POST "$BASE_URL/api/v1/precos/servicos" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"codigo": "SRV-LIMPEZA-INJETORES", "nome": "Limpeza de bicos injetores", "descricao": "Limpeza dos bicos injetores com ultrassom e teste de vazao", "preco": "210.00"}'
curl -sk "$BASE_URL/api/v1/precos/servicos?offset=0&limit=20" -H "Authorization: Bearer $TOKEN"
curl -sk "$BASE_URL/api/v1/precos/servicos/$CODIGO" -H "Authorization: Bearer $TOKEN"
curl -sk -X PUT "$BASE_URL/api/v1/precos/servicos/$CODIGO" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"nome": "Limpeza de bicos injetores", "descricao": "Limpeza dos bicos com ultrassom e teste de vazao", "preco": "230.00", "ativo": true}'
curl -sk -X DELETE "$BASE_URL/api/v1/precos/servicos/$CODIGO" -H "Authorization: Bearer $TOKEN"
```

As peças seguem o mesmo molde, com o `sku` no lugar do `codigo` e sem a descrição. O `sku` é o mesmo do estoque da Execução:

```bash
export SKU=PEC-CORREIA-DENTADA
curl -sk -X POST "$BASE_URL/api/v1/precos/pecas" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"sku": "PEC-CORREIA-DENTADA", "nome": "Correia dentada", "preco": "89.90"}'
curl -sk "$BASE_URL/api/v1/precos/pecas?offset=0&limit=20" -H "Authorization: Bearer $TOKEN"
curl -sk "$BASE_URL/api/v1/precos/pecas/$SKU" -H "Authorization: Bearer $TOKEN"
curl -sk -X PUT "$BASE_URL/api/v1/precos/pecas/$SKU" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"nome": "Correia dentada (kit)", "preco": "99.90", "ativo": true}'
curl -sk -X DELETE "$BASE_URL/api/v1/precos/pecas/$SKU" -H "Authorization: Bearer $TOKEN"
```

A validação que a Execução faz ao concluir o diagnóstico devolve os códigos inexistentes ou inativos, na ordem pedida e sem repetição, e a lista vazia quando todos valem. Um código fora do formato é só mais um inválido, e não um 422:

```bash
curl -sk -X POST "$BASE_URL/api/v1/precos/validacao" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"servicos": ["SRV-FREIOS"], "pecas": ["PEC-VELA", "PEC-X"]}'
```

## Orçamento

A consulta do atendente. O orçamento inteiro, com `ordem_id`, as linhas de preço congelado, o prazo e a decisão (quando há, `decidido_por` é o `sub` do atendente):

```bash
export ORCAMENTO_ID=$(curl -sk "$BASE_URL/api/v1/orcamentos?ordem_id=$ORDEM_ID" -H "Authorization: Bearer $TOKEN" | jq -r '.[0].id')
curl -sk "$BASE_URL/api/v1/orcamentos/$ORCAMENTO_ID" -H "Authorization: Bearer $TOKEN"
```

A decisão em nome do cliente, pelo atendente. O orçamento aceita uma decisão só: já aprovado ou recusado (inclusive pelo link, abaixo) responde 409, e expirado ou com o prazo esgotado, 410:

```bash
curl -sk -X POST "$BASE_URL/api/v1/orcamentos/$ORCAMENTO_ID/decisao" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"decisao": "aprovar"}'
```

## Link público de decisão

O cliente decide sem login. O token é o último trecho do `link_decisao` que o OS Service guarda no resumo do orçamento da OS. Ele é uma credencial: quem o tem decide o orçamento. A consulta mostra só o orçamento ainda pendente, e qualquer falha do token (adulterado, expirado, orçamento já decidido) é o mesmo 404:

```bash
export TOKEN_DO_LINK=$(curl -sk "https://localhost/os/api/v1/ordens-de-servico/$ORDEM_ID" -H "Authorization: Bearer $TOKEN" | jq -r '.orcamento.link_decisao | split("/") | last')
curl -sk "$BASE_URL/api/v1/publico/orcamentos/$TOKEN_DO_LINK"
curl -sk -X POST "$BASE_URL/api/v1/publico/orcamentos/$TOKEN_DO_LINK/decisao" \
  -H 'Content-Type: application/json' -d '{"decisao": "aprovar"}'
```

## Pagamento

Com o orçamento aprovado, a saga reserva as peças e manda o `SolicitarPagamento`; o `pagamento_id` está no resumo do pagamento da OS. A consulta do atendente devolve o pagamento inteiro: as tentativas que o provedor mostrou, as recusas contadas, os estornos automáticos e o `checkout_url`, que carrega o token do checkout simulado:

```bash
export PAGAMENTO_ID=$(curl -sk "https://localhost/os/api/v1/ordens-de-servico/$ORDEM_ID" -H "Authorization: Bearer $TOKEN" | jq -r .pagamento.pagamento_id)
curl -sk "$BASE_URL/api/v1/pagamentos/$PAGAMENTO_ID" -H "Authorization: Bearer $TOKEN"
```

## Webhook do Mercado Pago

O Mercado Pago avisa cada tentativa em `POST /api/v1/webhooks/mercadopago?data.id=<tentativa>&type=payment`, com o `x-signature` e o `x-request-id`. O corpo JSON é obrigatório, e só o `data.id` da query conta: o status vem da consulta ao provedor. A assinatura é o HMAC-SHA256, com o `MP_WEBHOOK_SECRET`, do manifesto `id:<data.id em minúsculas>;request-id:<x-request-id>;ts:<ts>;`; o bloco abaixo lê a chave da variável de ambiente, e não de um argumento, que ficaria na lista de processos. Sem a assinatura certa a resposta é 401, e sem o `MP_WEBHOOK_SECRET` no servidor, 503: no kind o Secret `billing-mercadopago` não existe, então a rota só responde 200 no compose, com a variável definida. Notificação sem `data.id` responde 200 com `{"processado": false}` antes de a assinatura ser conferida; a de outro tipo e a de pagamento que o provedor não conhece respondem o mesmo 200 só com a assinatura certa, e sem ela, 401:

```bash
export REFERENCIA=1234567890
export TS=$(date +%s)
export REQUISICAO=$(uuidgen | tr 'A-Z' 'a-z')
export ASSINATURA=$(python3 -c 'import hashlib, hmac, os, sys; print(hmac.new(os.environ["MP_WEBHOOK_SECRET"].encode(), sys.argv[1].encode(), hashlib.sha256).hexdigest())' "id:$REFERENCIA;request-id:$REQUISICAO;ts:$TS;")
curl -sk -X POST "$BASE_URL/api/v1/webhooks/mercadopago?data.id=$REFERENCIA&type=payment" \
  -H "x-signature: ts=$TS,v1=$ASSINATURA" -H "x-request-id: $REQUISICAO" \
  -H 'Content-Type: application/json' -d '{"type": "payment"}'
```

## Simulador do checkout

Só com `MP_MODE=simulado` (o padrão do kind e do compose): faz o papel do cliente pagando na página do Mercado Pago. O token é o parâmetro `token` do `checkout_url`, que vale até o fim do prazo da cobrança; token ausente, inválido, expirado ou de outro pagamento é o mesmo 404. Recusar conta uma recusa do provedor (a terceira, por padrão, encerra a cobrança), aprovar confirma, e pagamento que já saiu de `SOLICITADO` responde 409:

```bash
export TOKEN_DO_CHECKOUT=$(curl -sk "$BASE_URL/api/v1/pagamentos/$PAGAMENTO_ID" -H "Authorization: Bearer $TOKEN" | jq -r '.checkout_url | split("token=") | last')
curl -sk "$BASE_URL/simulador/checkout/$PAGAMENTO_ID?token=$TOKEN_DO_CHECKOUT"
curl -sk -X POST "$BASE_URL/api/v1/simulador/pagamentos/$PAGAMENTO_ID/recusar?token=$TOKEN_DO_CHECKOUT"
curl -sk -X POST "$BASE_URL/api/v1/simulador/pagamentos/$PAGAMENTO_ID/aprovar?token=$TOKEN_DO_CHECKOUT"
```
