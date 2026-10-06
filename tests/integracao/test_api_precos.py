"""API da tabela de precos: CRUD do admin, leitura interna e validacao."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

import jwt
import pytest
from fastapi.testclient import TestClient

from src.main import criar_app
from tests.integracao.apoio import configuracao

if TYPE_CHECKING:
    from collections.abc import Callable

    Cabecalhos = Callable[[str], dict[str, str]]

SERVICO = {
    "codigo": "SRV-TROCA-OLEO",
    "nome": "Troca de oleo",
    "descricao": "Troca do oleo do motor",
    "preco": "120.00",
}
PECA = {"sku": "PEC-OLEO-5W30", "nome": "Oleo 5W30 (1 L)", "preco": "45.00"}


def test_crud_de_servicos(api: TestClient, cabecalhos: Cabecalhos) -> None:
    admin = cabecalhos("admin")

    criado = api.post("/api/v1/precos/servicos", json=SERVICO, headers=admin)
    assert criado.status_code == 201
    assert criado.json() == {**SERVICO, "moeda": "BRL", "ativo": True}

    lido = api.get(
        "/api/v1/precos/servicos/SRV-TROCA-OLEO", headers=cabecalhos("mecanico")
    )
    assert lido.json()["preco"] == "120.00"  # dinheiro como string decimal

    atualizado = api.put(
        "/api/v1/precos/servicos/SRV-TROCA-OLEO",
        json={
            "nome": "Troca de oleo",
            "descricao": "Nova",
            "preco": "130.50",
            "ativo": True,
        },
        headers=admin,
    )
    assert atualizado.status_code == 200
    assert atualizado.json()["preco"] == "130.50"

    removido = api.delete("/api/v1/precos/servicos/SRV-TROCA-OLEO", headers=admin)
    assert removido.status_code == 204
    lista = api.get("/api/v1/precos/servicos", headers=cabecalhos("atendente"))
    assert lista.json() == {
        "items": [
            {
                **SERVICO,
                "descricao": "Nova",
                "preco": "130.50",
                "moeda": "BRL",
                "ativo": False,
            }
        ],
        "total": 1,
        "offset": 0,
        "limit": 20,
    }


def test_put_e_substituicao_completa(api: TestClient, cabecalhos: Cabecalhos) -> None:
    admin = cabecalhos("admin")
    api.post("/api/v1/precos/servicos", json=SERVICO, headers=admin)
    sem_ativo = api.put(
        "/api/v1/precos/servicos/SRV-TROCA-OLEO",
        json={"nome": "Troca", "descricao": "d", "preco": "130.00"},
        headers=admin,
    )
    assert sem_ativo.status_code == 422


def test_crud_de_pecas(api: TestClient, cabecalhos: Cabecalhos) -> None:
    admin = cabecalhos("admin")
    assert api.post("/api/v1/precos/pecas", json=PECA, headers=admin).status_code == 201
    assert (
        api.delete("/api/v1/precos/pecas/PEC-OLEO-5W30", headers=admin).status_code
        == 204
    )

    reativada = api.put(
        "/api/v1/precos/pecas/PEC-OLEO-5W30",
        json={"nome": "Oleo 5W30", "preco": "47.90", "ativo": True},
        headers=admin,
    )
    assert reativada.json() == {
        "sku": "PEC-OLEO-5W30",
        "nome": "Oleo 5W30",
        "preco": "47.90",
        "moeda": "BRL",
        "ativo": True,
    }
    assert (
        api.get("/api/v1/precos/pecas/PEC-OLEO-5W30", headers=admin).status_code == 200
    )
    pagina = api.get("/api/v1/precos/pecas?offset=1&limit=5", headers=admin).json()
    assert (pagina["items"], pagina["total"], pagina["offset"]) == ([], 1, 1)


@pytest.mark.parametrize(
    "consulta",
    ["offset=-1", "offset=1000001", f"offset={2**63}", "limit=0", "limit=101"],
)
def test_paginacao_fora_dos_limites_da_422(
    api: TestClient, cabecalhos: Cabecalhos, consulta: str
) -> None:
    resposta = api.get(f"/api/v1/precos/servicos?{consulta}", headers=cabecalhos())
    assert resposta.status_code == 422


def test_codigo_repetido_da_409(api: TestClient, cabecalhos: Cabecalhos) -> None:
    admin = cabecalhos("admin")
    api.post("/api/v1/precos/servicos", json=SERVICO, headers=admin)
    resposta = api.post("/api/v1/precos/servicos", json=SERVICO, headers=admin)
    assert resposta.status_code == 409
    assert resposta.json()["erro"]["codigo"] == "PRECO_JA_CADASTRADO"
    api.post("/api/v1/precos/pecas", json=PECA, headers=admin)
    assert api.post("/api/v1/precos/pecas", json=PECA, headers=admin).status_code == 409


@pytest.mark.parametrize(
    "caminho",
    [
        "/api/v1/precos/servicos/SRV-NAO-EXISTE",
        "/api/v1/precos/pecas/PEC-NAO-EXISTE",
    ],
)
def test_codigo_desconhecido_da_404(
    api: TestClient, cabecalhos: Cabecalhos, caminho: str
) -> None:
    resposta = api.get(caminho, headers=cabecalhos("admin"))
    assert resposta.status_code == 404
    assert resposta.json()["erro"]["codigo"] == "PRECO_NAO_ENCONTRADO"
    assert api.delete(caminho, headers=cabecalhos("admin")).status_code == 404


@pytest.mark.parametrize(
    "corpo",
    [
        {**SERVICO, "codigo": "srv minusculo"},
        {**SERVICO, "preco": "0"},
        {**SERVICO, "preco": "10.999"},
        {**SERVICO, "nome": ""},
        {**SERVICO, "extra": 1},
    ],
)
def test_entrada_invalida_da_422(
    api: TestClient, cabecalhos: Cabecalhos, corpo: dict[str, object]
) -> None:
    resposta = api.post(
        "/api/v1/precos/servicos", json=corpo, headers=cabecalhos("admin")
    )
    assert resposta.status_code == 422
    corpo = resposta.json()
    assert corpo["id_requisicao"] == resposta.headers["X-Request-ID"]
    assert corpo["detail"]
    assert all(set(item) == {"type", "loc", "msg"} for item in corpo["detail"])


@pytest.mark.parametrize("papel", ["atendente", "mecanico"])
def test_escrita_so_para_admin(
    api: TestClient, cabecalhos: Cabecalhos, papel: str
) -> None:
    resposta = api.post(
        "/api/v1/precos/servicos", json=SERVICO, headers=cabecalhos(papel)
    )
    assert resposta.status_code == 403
    assert resposta.json()["erro"]["codigo"] == "ACESSO_NEGADO"


def test_sem_token_da_401_com_envelope(api: TestClient) -> None:
    resposta = api.get("/api/v1/precos/servicos")
    assert resposta.status_code == 401
    assert resposta.headers["WWW-Authenticate"] == "Bearer"
    assert resposta.json()["erro"] == {
        "codigo": "NAO_AUTENTICADO",
        "mensagem": "Credencial ausente, invalida ou expirada",
        "id_requisicao": resposta.headers["X-Request-ID"],
    }


def test_validacao_de_itens_para_a_execucao(
    api: TestClient, cabecalhos: Cabecalhos
) -> None:
    admin = cabecalhos("admin")
    api.post("/api/v1/precos/servicos", json=SERVICO, headers=admin)
    api.post("/api/v1/precos/pecas", json=PECA, headers=admin)
    api.post(
        "/api/v1/precos/pecas",
        json={"sku": "PEC-VELA", "nome": "Vela", "preco": "28.00"},
        headers=admin,
    )
    api.delete("/api/v1/precos/pecas/PEC-VELA", headers=admin)

    resposta = api.post(
        "/api/v1/precos/validacao",
        json={
            "servicos": ["SRV-TROCA-OLEO", "SRV-NAO-EXISTE"],
            "pecas": ["PEC-OLEO-5W30", "PEC-VELA", "lixo qualquer"],
        },
        headers=cabecalhos("mecanico"),
    )

    assert resposta.status_code == 200
    assert resposta.json() == {
        "invalidos": ["SRV-NAO-EXISTE", "PEC-VELA", "lixo qualquer"]
    }
    tudo_valido = api.post(
        "/api/v1/precos/validacao",
        json={"servicos": ["SRV-TROCA-OLEO"]},
        headers=cabecalhos("mecanico"),
    )
    assert tudo_valido.json() == {"invalidos": []}


def test_validacao_nao_e_para_atendente(
    api: TestClient, cabecalhos: Cabecalhos
) -> None:
    resposta = api.post(
        "/api/v1/precos/validacao", json={}, headers=cabecalhos("atendente")
    )
    assert resposta.status_code == 403


def test_jwks_pendurado_nao_atrasa_rota_publica_nem_prende_requests(
    banco: Any,
    monkeypatch: pytest.MonkeyPatch,
    emitir_token: Callable[..., str],
) -> None:
    """OS aceita a conexao e nao responde: a busca do JWKS fica pendurada."""
    liberar = threading.Event()
    pendurou = threading.Event()

    def pendurar(_cliente: object) -> None:
        pendurou.set()
        liberar.wait(timeout=30)
        msg = "timed out"
        raise jwt.PyJWKClientConnectionError(msg)

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", pendurar)
    autorizado = {"Authorization": f"Bearer {emitir_token('admin')}"}
    with TestClient(criar_app(configuracao(), banco=banco)) as cliente:
        primeira = threading.Thread(
            target=cliente.get,
            args=("/api/v1/precos/servicos",),
            kwargs={"headers": autorizado},
        )
        primeira.start()
        try:
            assert pendurou.wait(timeout=5)
            inicio = time.monotonic()
            publica = cliente.get("/api/v1/publico/orcamentos/nao-e-token")
            assert publica.status_code == 404
            assert time.monotonic() - inicio < 1
            # Sem chave em cache: espera no maximo o timeout da busca (2 s), nao
            # a busca pendurada, e responde 503 com Retry-After.
            inicio = time.monotonic()
            interna = cliente.get("/api/v1/precos/servicos", headers=autorizado)
            assert interna.status_code == 503
            assert "Retry-After" in interna.headers
            assert time.monotonic() - inicio < 3
        finally:
            liberar.set()
            primeira.join(timeout=10)


def test_escrita_de_preco_deixa_log_de_auditoria(
    api: TestClient, cabecalhos: Cabecalhos, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        api.post("/api/v1/precos/servicos", json=SERVICO, headers=cabecalhos("admin"))
        api.delete(
            "/api/v1/precos/servicos/SRV-TROCA-OLEO", headers=cabecalhos("admin")
        )
    auditoria = [m for m in caplog.messages if "audit_price_changed" in m]
    assert len(auditoria) == 2
    assert all("usuario-teste" in m and "SRV-TROCA-OLEO" in m for m in auditoria)
    assert "cadastrar_servico" in auditoria[0]
    assert "desativar_servico" in auditoria[1]
