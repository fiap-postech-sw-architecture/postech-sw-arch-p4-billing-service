"""Autenticacao e papel por rota, guiados pela tabela de rotas do OpenAPI.

Rota nova fora da lista publica precisa entrar na matriz abaixo: o teste
falha ate alguem decidir quais papeis a acessam (ADR-039).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

PUBLICAS = {
    ("GET", "/api/v1/saude"),
    ("GET", "/api/v1/saude/pronto"),
    ("GET", "/api/v1/publico/orcamentos/{token}"),
    ("POST", "/api/v1/publico/orcamentos/{token}/decisao"),
    ("POST", "/api/v1/webhooks/mercadopago"),
    ("GET", "/simulador/checkout/{pagamento_id}"),
    ("POST", "/api/v1/simulador/pagamentos/{pagamento_id}/aprovar"),
    ("POST", "/api/v1/simulador/pagamentos/{pagamento_id}/recusar"),
}

ADMIN = frozenset({"admin"})
INTERNO = frozenset({"admin", "atendente", "mecanico"})
ATENDENTE = frozenset({"admin", "atendente"})
MECANICO = frozenset({"admin", "mecanico"})

# Operacao -> papeis que passam (admin herda todos).
MATRIZ: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "/api/v1/precos/servicos"): ADMIN,
    ("GET", "/api/v1/precos/servicos"): INTERNO,
    ("GET", "/api/v1/precos/servicos/{codigo}"): INTERNO,
    ("PUT", "/api/v1/precos/servicos/{codigo}"): ADMIN,
    ("DELETE", "/api/v1/precos/servicos/{codigo}"): ADMIN,
    ("POST", "/api/v1/precos/pecas"): ADMIN,
    ("GET", "/api/v1/precos/pecas"): INTERNO,
    ("GET", "/api/v1/precos/pecas/{sku}"): INTERNO,
    ("PUT", "/api/v1/precos/pecas/{sku}"): ADMIN,
    ("DELETE", "/api/v1/precos/pecas/{sku}"): ADMIN,
    ("POST", "/api/v1/precos/validacao"): MECANICO,
    ("GET", "/api/v1/orcamentos"): ATENDENTE,
    ("GET", "/api/v1/orcamentos/{orcamento_id}"): ATENDENTE,
    ("POST", "/api/v1/orcamentos/{orcamento_id}/decisao"): ATENDENTE,
    ("GET", "/api/v1/pagamentos/{pagamento_id}"): ATENDENTE,
}

_ID_FIXO = "0e3e4a2b-3c0a-4f5e-8d6e-1b2c3d4e5f60"


def _url(caminho: str) -> str:
    return (
        caminho.replace("{orcamento_id}", _ID_FIXO)
        .replace("{pagamento_id}", _ID_FIXO)
        .replace("{codigo}", "SRV-X")
        .replace("{sku}", "PEC-X")
    )


def _operacoes(app: FastAPI) -> set[tuple[str, str]]:
    return {
        (metodo.upper(), caminho)
        for caminho, operacoes in app.openapi()["paths"].items()
        for metodo in operacoes
    }


def test_toda_rota_esta_na_lista_publica_ou_na_matriz(app: FastAPI) -> None:
    assert _operacoes(app) - PUBLICAS == set(MATRIZ)


@pytest.mark.parametrize(
    ("metodo", "caminho"), list(MATRIZ), ids=[f"{m} {c}" for m, c in MATRIZ]
)
def test_rota_interna_sem_token_e_401(
    api: TestClient, metodo: str, caminho: str
) -> None:
    resposta = api.request(metodo, _url(caminho), json={})
    assert resposta.status_code == 401
    assert resposta.headers["WWW-Authenticate"] == "Bearer"
    assert resposta.json()["erro"]["codigo"] == "NAO_AUTENTICADO"


CASOS = [
    (metodo, caminho, papel)
    for (metodo, caminho) in MATRIZ
    for papel in ("admin", "atendente", "mecanico")
]


@pytest.mark.parametrize(
    ("metodo", "caminho", "papel"),
    CASOS,
    ids=[f"{m} {c} {p}" for m, c, p in CASOS],
)
def test_matriz_de_papeis(
    api: TestClient,
    cabecalhos: Callable[[str], dict[str, str]],
    metodo: str,
    caminho: str,
    papel: str,
) -> None:
    resposta = api.request(metodo, _url(caminho), json={}, headers=cabecalhos(papel))
    if papel in MATRIZ[(metodo, caminho)]:
        # Passou da autorizacao (o resto pode ser 404/409/422 pelo corpo vazio).
        assert resposta.status_code not in {401, 403}
    else:
        assert resposta.status_code == 403
        assert resposta.json()["erro"]["codigo"] == "ACESSO_NEGADO"
