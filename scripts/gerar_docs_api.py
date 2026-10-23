"""Gera os documentos da API em docs/api a partir do proprio app (make docs-api).

- ``docs/api/openapi.json``: o ``app.openapi()`` do servico, o mesmo do ``/docs``,
  com o simulador de pagamento ligado (``MP_MODE=simulado``): com o Mercado Pago
  real as tres rotas do simulador nao existem.
- ``docs/api/billing.postman_collection.json``: colecao Postman v2.1 com uma
  requisicao por metodo e rota do OpenAPI, cada uma com corpo e resposta de
  exemplo (``EXEMPLOS``), a variavel ``{{baseUrl}}`` e o token bearer em
  ``{{token}}``; a criacao de servico e de peca e a consulta do orcamento por
  ordem guardam o codigo, o SKU e o id do orcamento num script de teste.

Os dois saem juntos, do mesmo OpenAPI, para nao divergirem. O script so monta o
app e le o OpenAPI dele: nao sobe servidor nem banco. ``tests/unitarios/
test_documentacao.py`` reprova quando os arquivos ficam atras do codigo, quando
falta exemplo para uma rota, quando um exemplo sai do schema da rota ou quando a
colecao perde o token, o ``noauth`` das sondas e das rotas publicas, o
``Content-Type`` dos corpos, o ``{{baseUrl}}``, a resposta de exemplo ou os
scripts de teste.
"""

from __future__ import annotations

import json
import re
import sys
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

if TYPE_CHECKING:
    from fastapi import FastAPI

RAIZ = Path(__file__).resolve().parents[1]
DOCS_API = RAIZ / "docs" / "api"
OPENAPI = DOCS_API / "openapi.json"
COLECAO = DOCS_API / "billing.postman_collection.json"

ESQUEMA_POSTMAN = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
DESCRICAO = (
    "Uma requisicao por rota do Billing Service, com corpo e resposta de exemplo. "
    "baseUrl aponta para o Kong do kind (https://localhost/billing; o certificado "
    "e autoassinado: desligue a verificacao de SSL do Postman) e pode ser trocada "
    "pelo compose do servico (http://127.0.0.1:8002), onde so as sondas, o link "
    "publico, o webhook e o simulador respondem sem o OS Service. O Billing nao "
    "tem login: token e o access_token do POST /api/v1/autenticacao/login do OS "
    "Service (admin para a tabela de precos; atendente para o orcamento e o "
    "pagamento; mecanico para a validacao de itens), colado a mao. Os ids do "
    "orcamento e do pagamento nascem na saga, nao nesta API: ordem_id, "
    "pagamento_id, token_do_link e token_do_checkout vem de fora e ficam com o "
    "valor do exemplo ate serem trocados. ordem_id e pagamento_id saem da OS "
    "aberta no OS Service (GET /api/v1/ordens-de-servico/{id}: orcamento."
    "link_decisao tem o token_do_link no fim do caminho, e pagamento.pagamento_id "
    "e o pagamento_id); token_do_checkout e o parametro token do checkout_url do "
    "pagamento, que so existe com o simulador. codigo e sku sao guardados pelo "
    "cadastro de servico e de peca, e orcamento_id pela consulta do orcamento por "
    "ordem (o script de teste de cada uma o grava). O webhook so responde com "
    "MP_WEBHOOK_SECRET no servidor, e o x-signature do exemplo e um molde: o "
    "HMAC-SHA256 do manifesto esta em docs/api/exemplos.md, em Webhook do "
    "Mercado Pago. Gerada por "
    "scripts/gerar_docs_api.py (make docs-api); o OpenAPI esta em "
    "docs/api/openapi.json."
)
# Os tokens daqui sao variaveis vazias ou moldes da colecao, nao credenciais. O
# bandit (B105) os tomaria por senha pelo nome da chave, mas o `make security`
# so varre src/.
VARIAVEIS = {
    "baseUrl": "https://localhost/billing",
    "token": "",
    "ordem_id": "3c9a7e10-5b2f-4f6d-8a41-0e2d9b7c6f55",
    "orcamento_id": "c2a9e3f4-6b1d-4e8a-9f20-7d3c5b8a1e64",
    "pagamento_id": "5e8b1f2a-3c4d-4a6b-8e9f-0a1b2c3d4e5f",
    "token_do_link": "<token do link de decisao>",
    "token_do_checkout": "<token do checkout simulado>",
    "codigo": "SRV-LIMPEZA-INJETORES",
    "sku": "PEC-CORREIA-DENTADA",
}
# token, ordem_id, pagamento_id, token_do_link e token_do_checkout nao saem de
# resposta nenhuma da API do Billing: a DESCRICAO diz de onde vem cada um.
# codigo, sku e orcamento_id sao guardados por script de teste (`guarda`, em
# EXEMPLOS).
# O parametro `token` do caminho publico e o do link de decisao; `{{token}}` e o
# bearer da colecao.
PARAMETRO_DA_ROTA = {"token": "token_do_link"}

_ORDEM = VARIAVEIS["ordem_id"]
_ORCAMENTO = VARIAVEIS["orcamento_id"]
_PAGAMENTO = VARIAVEIS["pagamento_id"]
_ATENDENTE = "9809e49f-24a6-4e03-a4bf-c655feb2a0dd"
_DECISAO = {"decisao": "aprovar"}
_SERVICO_NOVO = {
    "codigo": VARIAVEIS["codigo"],
    "nome": "Limpeza de bicos injetores",
    "descricao": "Limpeza dos bicos injetores com ultrassom e teste de vazao",
    "preco": "210.00",
}
_SERVICO = {
    "codigo": VARIAVEIS["codigo"],
    "nome": "Limpeza de bicos injetores",
    "descricao": "Limpeza dos bicos injetores com ultrassom e teste de vazao",
    "preco": "210.00",
    "moeda": "BRL",
    "ativo": True,
}
_PECA_NOVA = {
    "sku": VARIAVEIS["sku"],
    "nome": "Correia dentada",
    "preco": "89.90",
}
_PECA = {**_PECA_NOVA, "moeda": "BRL", "ativo": True}
_LINHAS = [
    {
        "tipo": "servico",
        "codigo": "SRV-FREIOS",
        "descricao": "Revisao do sistema de freios",
        "quantidade": 1,
        "preco_unitario": "250.00",
        "subtotal": "250.00",
    },
    {
        "tipo": "peca",
        "codigo": "PEC-PASTILHA-FREIO",
        "descricao": "Jogo de pastilhas de freio",
        "quantidade": 2,
        "preco_unitario": "160.00",
        "subtotal": "320.00",
    },
]
_ORCAMENTO_PENDENTE = {
    "id": _ORCAMENTO,
    "ordem_id": _ORDEM,
    "status": "PENDENTE",
    "linhas": _LINHAS,
    "total": "570.00",
    "moeda": "BRL",
    "criado_em": "2026-10-18T13:00:00Z",
    "valido_ate": "2026-10-21T13:00:00Z",
    "decisao": None,
    "motivo_cancelamento": None,
}
_ORCAMENTO_APROVADO = {
    **_ORCAMENTO_PENDENTE,
    "status": "APROVADO",
    "decisao": {
        "canal": "atendente",
        "decidido_em": "2026-10-18T13:10:00Z",
        "decidido_por": _ATENDENTE,
    },
}
_ORCAMENTO_PUBLICO = {
    "id": _ORCAMENTO,
    "status": "PENDENTE",
    "linhas": _LINHAS,
    "total": "570.00",
    "moeda": "BRL",
    "valido_ate": "2026-10-21T13:00:00Z",
    "decisao": None,
}
_PAGAMENTO_SOLICITADO = {
    "id": _PAGAMENTO,
    "ordem_id": _ORDEM,
    "status": "SOLICITADO",
    "orcamento_id": _ORCAMENTO,
    "valor": "570.00",
    "moeda": "BRL",
    "provedor": "simulado",
    "referencia_preferencia": f"sim-pref-{_PAGAMENTO}",
    "checkout_url": (
        f"https://localhost/billing/simulador/checkout/{_PAGAMENTO}?token=<token>"
    ),
    "expira_em": "2026-10-18T14:10:00Z",
    "criado_em": "2026-10-18T13:10:00Z",
    "recusas": 0,
    "referencia_pagamento": None,
    "confirmado_em": None,
    "encerrado_em": None,
    "motivo": None,
    "estornado_em": None,
    "motivo_estorno": None,
    "notificacoes": [],
    "estornos_automaticos": [],
}
_TENTATIVA_RECUSADA = {
    "recebida_em": "2026-10-18T13:12:00Z",
    "referencia_pagamento": "sim-3f9a1c0e8b7d4a6e9c2d5b1a7e4f8c30",
    "status_provedor": "rejected",
}
_TENTATIVA_APROVADA = {
    "recebida_em": "2026-10-18T13:14:00Z",
    "referencia_pagamento": "sim-1062157c80674759b93800efcd0b41bf",
    "status_provedor": "approved",
}
_PAGAMENTO_CONFIRMADO = {
    **_PAGAMENTO_SOLICITADO,
    "status": "CONFIRMADO",
    "recusas": 1,
    "referencia_pagamento": _TENTATIVA_APROVADA["referencia_pagamento"],
    "confirmado_em": "2026-10-18T13:14:00Z",
    "notificacoes": [_TENTATIVA_RECUSADA, _TENTATIVA_APROVADA],
}


def _pagina(item: dict[str, Any]) -> dict[str, Any]:
    return {"items": [item], "total": 1, "offset": 0, "limit": 20}


_PAGINACAO = {"offset": 0, "limit": 20}
_JSON = {"Content-Type": "application/json"}
# A pagina de checkout que a rota do simulador devolve (router_simulador.py), com
# os valores da colecao; os botoes levam o token da requisicao, escapado na URL.
# tests/unitarios/test_documentacao.py a compara com a resposta real da rota.
_PAGINA_DO_CHECKOUT = """<!doctype html>
<html lang="pt-BR">
<head><meta charset="utf-8"><title>Checkout simulado - PytStop</title></head>
<body>
<h1>Checkout simulado</h1>
<p>Ambiente de simulacao: nenhuma cobranca real e feita.</p>
<dl>
<dt>Pagamento</dt><dd>{pagamento}</dd>
<dt>Valor</dt><dd>BRL 570.00</dd>
<dt>Status</dt><dd>SOLICITADO</dd>
</dl>
<form method="post" action="{acoes}/aprovar?token={token}">
<button type="submit">Aprovar</button></form>
<form method="post" action="{acoes}/recusar?token={token}">
<button type="submit">Recusar</button></form>
</body>
</html>
""".format(
    pagamento=_PAGAMENTO,
    acoes=f"{VARIAVEIS['baseUrl']}/api/v1/simulador/pagamentos/{_PAGAMENTO}",
    token=quote(VARIAVEIS["token_do_checkout"], safe=""),
)

# Ordem dos fluxos de docs/api/exemplos.md; as chaves sao as do OpenAPI (metodo,
# rota). Cada exemplo tem `status` e, quando a rota os tem, `corpo`, `query`,
# `cabecalhos` (alem do Content-Type), `resposta` (JSON) ou `html` (a pagina do
# checkout) e `guarda` (variavel -> acesso ao campo da resposta que o script de
# teste grava).
EXEMPLOS: dict[tuple[str, str], dict[str, Any]] = {
    ("get", "/api/v1/saude"): {
        "status": 200,
        "resposta": {"status": "ok", "modo": "simulado"},
    },
    ("get", "/api/v1/saude/pronto"): {"status": 200, "resposta": {"status": "pronto"}},
    ("post", "/api/v1/precos/servicos"): {
        "corpo": _SERVICO_NOVO,
        "status": 201,
        "resposta": _SERVICO,
        "guarda": {"codigo": ".codigo"},
    },
    ("get", "/api/v1/precos/servicos"): {
        "query": _PAGINACAO,
        "status": 200,
        "resposta": _pagina(_SERVICO),
    },
    ("get", "/api/v1/precos/servicos/{codigo}"): {"status": 200, "resposta": _SERVICO},
    ("put", "/api/v1/precos/servicos/{codigo}"): {
        "corpo": {
            "nome": "Limpeza de bicos injetores",
            "descricao": "Limpeza dos bicos com ultrassom e teste de vazao",
            "preco": "230.00",
            "ativo": True,
        },
        "status": 200,
        "resposta": {
            **_SERVICO,
            "descricao": "Limpeza dos bicos com ultrassom e teste de vazao",
            "preco": "230.00",
        },
    },
    ("delete", "/api/v1/precos/servicos/{codigo}"): {"status": 204},
    ("post", "/api/v1/precos/pecas"): {
        "corpo": _PECA_NOVA,
        "status": 201,
        "resposta": _PECA,
        "guarda": {"sku": ".sku"},
    },
    ("get", "/api/v1/precos/pecas"): {
        "query": _PAGINACAO,
        "status": 200,
        "resposta": _pagina(_PECA),
    },
    ("get", "/api/v1/precos/pecas/{sku}"): {"status": 200, "resposta": _PECA},
    ("put", "/api/v1/precos/pecas/{sku}"): {
        "corpo": {"nome": "Correia dentada (kit)", "preco": "99.90", "ativo": True},
        "status": 200,
        "resposta": {**_PECA, "nome": "Correia dentada (kit)", "preco": "99.90"},
    },
    ("delete", "/api/v1/precos/pecas/{sku}"): {"status": 204},
    ("post", "/api/v1/precos/validacao"): {
        "corpo": {"servicos": ["SRV-FREIOS"], "pecas": ["PEC-VELA", "PEC-X"]},
        "status": 200,
        "resposta": {"invalidos": ["PEC-X"]},
    },
    ("get", "/api/v1/orcamentos"): {
        "query": {"ordem_id": "{{ordem_id}}"},
        "status": 200,
        "resposta": [_ORCAMENTO_PENDENTE],
        "guarda": {"orcamento_id": "[0].id"},
    },
    ("get", "/api/v1/orcamentos/{orcamento_id}"): {
        "status": 200,
        "resposta": _ORCAMENTO_PENDENTE,
    },
    ("post", "/api/v1/orcamentos/{orcamento_id}/decisao"): {
        "corpo": _DECISAO,
        "status": 200,
        "resposta": _ORCAMENTO_APROVADO,
    },
    ("get", "/api/v1/publico/orcamentos/{token}"): {
        "status": 200,
        "resposta": _ORCAMENTO_PUBLICO,
    },
    ("post", "/api/v1/publico/orcamentos/{token}/decisao"): {
        "corpo": _DECISAO,
        "status": 200,
        "resposta": {
            **_ORCAMENTO_PUBLICO,
            "status": "APROVADO",
            "decisao": {
                "canal": "link",
                "decidido_em": "2026-10-18T13:10:00Z",
                "decidido_por": None,
            },
        },
    },
    ("get", "/api/v1/pagamentos/{pagamento_id}"): {
        "status": 200,
        "resposta": _PAGAMENTO_SOLICITADO,
    },
    ("post", "/api/v1/webhooks/mercadopago"): {
        "query": {"data.id": "1234567890", "type": "payment"},
        "cabecalhos": {
            "x-signature": "ts=1760780000,v1=<hmac-sha256 do manifesto>",
            "x-request-id": "5f0c7c5e-1b9e-4c3e-9a4e-2d8f6b1a7c11",
        },
        "corpo": {"type": "payment"},
        "status": 200,
        "resposta": {"processado": True},
    },
    ("get", "/simulador/checkout/{pagamento_id}"): {
        "query": {"token": "{{token_do_checkout}}"},
        "status": 200,
        "html": _PAGINA_DO_CHECKOUT,
    },
    ("post", "/api/v1/simulador/pagamentos/{pagamento_id}/aprovar"): {
        "query": {"token": "{{token_do_checkout}}"},
        "status": 200,
        "resposta": _PAGAMENTO_CONFIRMADO,
    },
    ("post", "/api/v1/simulador/pagamentos/{pagamento_id}/recusar"): {
        "query": {"token": "{{token_do_checkout}}"},
        "status": 200,
        "resposta": {
            **_PAGAMENTO_SOLICITADO,
            "recusas": 1,
            "notificacoes": [_TENTATIVA_RECUSADA],
        },
    },
}


def _json(documento: object) -> str:
    return json.dumps(documento, indent=2, ensure_ascii=False) + "\n"


def _caminho_da_colecao(rota: str) -> str:
    def variavel(achado: re.Match[str]) -> str:
        nome = achado[1]
        return "{{" + PARAMETRO_DA_ROTA.get(nome, nome) + "}}"

    return re.sub(r"\{(\w+)\}", variavel, rota)


def _requisicao(
    metodo: str, rota: str, operacao: dict[str, Any], exemplo: dict[str, Any]
) -> dict[str, Any]:
    caminho = _caminho_da_colecao(rota)
    query = exemplo.get("query", {})
    url: dict[str, Any] = {
        "raw": "{{baseUrl}}"
        + caminho
        + (f"?{urlencode(query, safe='{}')}" if query else ""),
        "host": ["{{baseUrl}}"],
        "path": caminho.strip("/").split("/"),
    }
    if query:
        url["query"] = [
            {"key": chave, "value": str(valor)} for chave, valor in query.items()
        ]
    cabecalhos = dict(exemplo.get("cabecalhos", {}))
    if "corpo" in exemplo:
        cabecalhos = {**_JSON, **cabecalhos}
    pedido: dict[str, Any] = {
        "method": metodo.upper(),
        "header": [{"key": k, "value": v} for k, v in cabecalhos.items()],
        "url": url,
        "description": operacao.get("description", operacao["summary"]),
    }
    # Rota sem seguranca no OpenAPI (sondas, link publico, webhook e simulador)
    # nao leva o token da colecao.
    if "security" not in operacao:
        pedido["auth"] = {"type": "noauth"}
    if "corpo" in exemplo:
        pedido["body"] = {
            "mode": "raw",
            "raw": _json(exemplo["corpo"]).rstrip("\n"),
            "options": {"raw": {"language": "json"}},
        }
    status = HTTPStatus(exemplo["status"])
    resposta: dict[str, Any] = {
        "name": f"{status.value} {status.phrase}",
        "originalRequest": pedido,
        "status": status.phrase,
        "code": status.value,
        "header": [],
        "body": "",
    }
    if "resposta" in exemplo:
        resposta["header"] = [{"key": "Content-Type", "value": "application/json"}]
        resposta["body"] = _json(exemplo["resposta"]).rstrip("\n")
        resposta["_postman_previewlanguage"] = "json"
    elif "html" in exemplo:
        resposta["header"] = [
            {"key": "Content-Type", "value": "text/html; charset=utf-8"}
        ]
        resposta["body"] = exemplo["html"]
        resposta["_postman_previewlanguage"] = "html"
    item: dict[str, Any] = {
        "name": operacao["summary"],
        "request": pedido,
        "response": [resposta],
    }
    if "guarda" in exemplo:
        item["event"] = [_script_de_teste(status.value, exemplo["guarda"])]
    return item


def _condicao(status: int, campo: str) -> str:
    """O status esperado e, no acesso por indice (``[0].id``), a lista nao vazia.

    A lista vazia e resposta valida (a consulta do orcamento antes de ele nascer),
    e ``[0].id`` nela levanta TypeError no script de teste.
    """
    condicao = f"pm.response.code === {status}"
    if campo.startswith("["):
        return f"{condicao} && pm.response.json().length"
    return condicao


def _script_de_teste(status: int, guarda: dict[str, str]) -> dict[str, Any]:
    """Guarda campos da resposta em variaveis da colecao, uma linha por variavel."""
    return {
        "listen": "test",
        "script": {
            "type": "text/javascript",
            "exec": [
                f"if ({_condicao(status, campo)}) pm.collectionVariables.set("
                f'"{variavel}", pm.response.json(){campo});'
                for variavel, campo in guarda.items()
            ],
        },
    }


def colecao(openapi: dict[str, Any]) -> dict[str, Any]:
    """Colecao Postman v2.1 do OpenAPI: uma pasta por tag, uma requisicao por rota.

    Raises:
        ValueError: ``EXEMPLOS`` nao cobre exatamente os pares do OpenAPI.
    """
    pares = {(metodo, rota) for rota, ops in openapi["paths"].items() for metodo in ops}
    if pares != EXEMPLOS.keys():
        faltam = sorted(pares - EXEMPLOS.keys())
        sobram = sorted(EXEMPLOS.keys() - pares)
        msg = f"EXEMPLOS fora do OpenAPI: faltam {faltam}, sobram {sobram}"
        raise ValueError(msg)
    pastas: dict[str, list[dict[str, Any]]] = {}
    for (metodo, rota), exemplo in EXEMPLOS.items():
        operacao = openapi["paths"][rota][metodo]
        pastas.setdefault(operacao["tags"][0], []).append(
            _requisicao(metodo, rota, operacao, exemplo)
        )
    return {
        "info": {
            "name": openapi["info"]["title"],
            "description": DESCRICAO,
            "schema": ESQUEMA_POSTMAN,
        },
        "auth": {
            "type": "bearer",
            "bearer": [{"key": "token", "value": "{{token}}", "type": "string"}],
        },
        "variable": [
            {"key": chave, "value": valor} for chave, valor in VARIAVEIS.items()
        ],
        "item": [{"name": nome, "item": itens} for nome, itens in pastas.items()],
    }


def app_da_documentacao() -> FastAPI:
    """O app da documentacao: o simulador ligado e um provider descartavel, que
    nao instala nada no processo. Nao conecta ao MongoDB (so o lifespan o faz)."""
    from opentelemetry.sdk.trace import TracerProvider

    from src.configuracao import Configuracao
    from src.main import criar_app as montar

    config = Configuracao.do_ambiente(
        {"ENVIRONMENT": "development", "MP_MODE": "simulado"}
    )
    return montar(config, provedor=TracerProvider(shutdown_on_exit=False))


def main() -> int:
    documento = app_da_documentacao().openapi()
    try:
        postman = colecao(documento)
    except ValueError as erro:
        print(erro, file=sys.stderr)
        return 1
    DOCS_API.mkdir(parents=True, exist_ok=True)
    OPENAPI.write_text(_json(documento), encoding="utf-8")
    COLECAO.write_text(_json(postman), encoding="utf-8")
    print(f"atualizados: {OPENAPI.relative_to(RAIZ)} e {COLECAO.relative_to(RAIZ)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
