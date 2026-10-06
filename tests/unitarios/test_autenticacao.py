"""Dependency de autenticacao: 401 uniforme, 403 so por papel, 503 do JWKS."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.compartilhado.infraestrutura.jwks import ValidadorDeTokenJWKS
from src.compartilhado.interfaces.autenticacao import (
    CREDENCIAL_INVALIDA,
    Papel,
    UsuarioAutenticado,
    exigir_papel,
)
from src.compartilhado.interfaces.error_handler import registrar_error_handlers
from tests.conftest import AUDIENCIA, EMISSOR, SUB_DO_TESTE

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def cliente(jwks_publicado: dict[str, Any]) -> TestClient:
    app = FastAPI()
    app.state.validador_de_token = ValidadorDeTokenJWKS(
        "http://os.teste/.well-known/jwks.json", emissor=EMISSOR, audiencia=AUDIENCIA
    )

    @app.get("/atendimento")
    def atendimento(
        usuario: Annotated[UsuarioAutenticado, Depends(exigir_papel(Papel.ATENDENTE))],
    ) -> dict[str, str]:
        return {"sub": usuario.sub, "papel": usuario.papel.value}

    registrar_error_handlers(app)
    return TestClient(app)


def chamar(cliente: TestClient, token: str | None) -> Any:
    cabecalhos = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return cliente.get("/atendimento", headers=cabecalhos)


@pytest.mark.parametrize("papel", ["atendente", "admin"])
def test_papel_permitido_passa_e_admin_herda(
    cliente: TestClient, emitir_token: Callable[..., str], papel: str
) -> None:
    resposta = chamar(cliente, emitir_token(papel))
    assert resposta.status_code == 200
    assert resposta.json() == {"sub": SUB_DO_TESTE, "papel": papel}


def test_papel_valido_insuficiente_e_403(
    cliente: TestClient, emitir_token: Callable[..., str]
) -> None:
    resposta = chamar(cliente, emitir_token("mecanico"))
    assert resposta.status_code == 403
    assert resposta.json()["erro"]["codigo"] == "ACESSO_NEGADO"


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"papel": None}, id="sem-papel"),
        pytest.param({"papel": "cliente"}, id="papel-desconhecido"),
        pytest.param({"papel": "ADMIN"}, id="papel-em-maiusculas"),
        pytest.param({"papel": ["admin"]}, id="papel-em-lista"),
        pytest.param({"type": "refresh"}, id="refresh-no-lugar-do-access"),
        pytest.param({"type": None}, id="sem-type"),
        pytest.param({"exp": 1}, id="expirado"),
        pytest.param({"exp": None}, id="sem-exp"),
        pytest.param({"iss": "outro"}, id="outro-emissor"),
        pytest.param({"sub": "usuario-sem-uuid"}, id="sub-que-nao-e-uuid"),
    ],
)
def test_toda_falha_de_credencial_e_o_mesmo_401(
    cliente: TestClient, emitir_token: Callable[..., str], claims: dict[str, Any]
) -> None:
    papel = claims.get("papel", "admin")
    outras = {nome: valor for nome, valor in claims.items() if nome != "papel"}
    resposta = chamar(cliente, emitir_token(papel, **outras))
    assert resposta.status_code == 401
    assert resposta.headers["WWW-Authenticate"] == "Bearer"
    erro = resposta.json()["erro"]
    assert (erro["codigo"], erro["mensagem"]) == (
        "NAO_AUTENTICADO",
        CREDENCIAL_INVALIDA,
    )


@pytest.mark.parametrize(
    "token", [None, "", "abc"], ids=["sem-cabecalho", "vazio", "malformado"]
)
def test_sem_token_ou_malformado_e_o_mesmo_401(
    cliente: TestClient, token: str | None
) -> None:
    resposta = chamar(cliente, token)
    assert resposta.status_code == 401
    assert resposta.json()["erro"]["mensagem"] == CREDENCIAL_INVALIDA


def test_jwks_indisponivel_e_503_com_retry_after(
    monkeypatch: pytest.MonkeyPatch, emitir_token: Callable[..., str]
) -> None:
    def fora_do_ar(_self: object) -> None:
        msg = "Fail to fetch data from the url"
        raise jwt.PyJWKClientConnectionError(msg)

    app = FastAPI()
    app.state.validador_de_token = ValidadorDeTokenJWKS(
        "http://os.teste/.well-known/jwks.json", emissor=EMISSOR, audiencia=AUDIENCIA
    )

    @app.get("/atendimento")
    def atendimento(
        _usuario: Annotated[UsuarioAutenticado, Depends(exigir_papel())],
    ) -> None:
        return None

    registrar_error_handlers(app)
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fora_do_ar)
    resposta = chamar(TestClient(app), emitir_token("admin"))
    assert resposta.status_code == 503
    assert resposta.headers["Retry-After"] == "5"
    assert resposta.json()["erro"]["codigo"] == "SERVICO_INDISPONIVEL"
