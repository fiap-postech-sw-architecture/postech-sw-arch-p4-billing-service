"""Validacao do JWT RS256 do OS Service pelo JWKS (chave RSA gerada no teste)."""

from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING, Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from src.compartilhado.interfaces.autenticacao import (
    Papel,
    UsuarioAutenticado,
    VerificadorDeToken,
)
from tests.conftest import AUDIENCIA, EMISSOR

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def verificador(jwks_publicado: dict[str, Any]) -> VerificadorDeToken:
    return VerificadorDeToken(
        jwks_url="http://os.teste/.well-known/jwks.json",
        emissor=EMISSOR,
        audiencia=AUDIENCIA,
    )


def falha(verificador: VerificadorDeToken, token: str) -> HTTPException:
    with pytest.raises(HTTPException) as erro:
        verificador.verificar(token)
    return erro.value


@pytest.mark.parametrize("papel", list(Papel))
def test_token_valido_devolve_usuario_e_papel(
    verificador: VerificadorDeToken, emitir_token: Callable[..., str], papel: Papel
) -> None:
    assert verificador.verificar(emitir_token(papel.value)) == UsuarioAutenticado(
        sub="usuario-teste", papel=papel
    )


def test_relogio_do_os_um_pouco_adiantado_e_tolerado(
    verificador: VerificadorDeToken, emitir_token: Callable[..., str]
) -> None:
    adiantado = int(time.time()) + 20
    usuario = verificador.verificar(emitir_token("mecanico", iat=adiantado))
    assert usuario.papel is Papel.MECANICO


def test_token_sem_tipo_e_aceito(
    verificador: VerificadorDeToken, emitir_token: Callable[..., str]
) -> None:
    usuario = verificador.verificar(emitir_token("atendente", type=None))
    assert usuario.papel is Papel.ATENDENTE


@pytest.mark.parametrize(
    ("claims", "mensagem"),
    [
        ({"exp": int(time.time()) - 60}, "Token expirado"),
        ({"iss": "outro-emissor"}, "Token invalido"),
        ({"aud": "outra-audiencia"}, "Token invalido"),
        ({"sub": None}, "Token invalido"),
        ({"type": "refresh"}, "Token nao e do tipo access"),
    ],
)
def test_claims_invalidas_dao_401(
    verificador: VerificadorDeToken,
    emitir_token: Callable[..., str],
    claims: dict[str, Any],
    mensagem: str,
) -> None:
    erro = falha(verificador, emitir_token("admin", **claims))
    assert (erro.status_code, erro.detail) == (401, mensagem)
    assert erro.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.parametrize("papel", [None, "cliente", "ADMIN", ["admin"]])
def test_papel_ausente_ou_desconhecido_da_403(
    verificador: VerificadorDeToken, emitir_token: Callable[..., str], papel: object
) -> None:
    erro = falha(verificador, emitir_token(papel))
    assert (erro.status_code, erro.detail) == (403, "Papel nao autorizado")


def test_assinatura_de_outra_chave_com_o_mesmo_kid_da_401(
    verificador: VerificadorDeToken, emitir_token: Callable[..., str]
) -> None:
    impostora = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    erro = falha(verificador, emitir_token("admin", chave=impostora))
    assert erro.status_code == 401


def test_kid_desconhecido_da_401(
    verificador: VerificadorDeToken, chave_rsa: rsa.RSAPrivateKey
) -> None:
    token = jwt.encode(
        {"sub": "x", "papel": "admin", "iss": EMISSOR, "aud": AUDIENCIA, "exp": 9e9},
        chave_rsa,
        algorithm="RS256",
        headers={"kid": "outra-chave"},
    )
    assert falha(verificador, token).status_code == 401


def test_token_hs256_nao_e_aceito(verificador: VerificadorDeToken) -> None:
    # Confusao de algoritmo: HS256 "assinado" com um segredo qualquer.
    token = jwt.encode(
        {"sub": "x", "papel": "admin", "iss": EMISSOR, "aud": AUDIENCIA, "exp": 9e9},
        secrets.token_hex(32),
        algorithm="HS256",
        headers={"kid": "chave-de-teste"},
    )
    assert falha(verificador, token).status_code == 401


@pytest.mark.parametrize("token", ["", "abc", "a.b.c", "eyJhbGciOiJSUzI1NiJ9.e30.c2ln"])
def test_token_malformado_da_401(verificador: VerificadorDeToken, token: str) -> None:
    assert falha(verificador, token).status_code == 401


def test_jwks_fora_do_ar_da_503(
    monkeypatch: pytest.MonkeyPatch, emitir_token: Callable[..., str]
) -> None:
    def fora_do_ar(_self: object) -> None:
        msg = "Fail to fetch data from the url"
        raise jwt.PyJWKClientConnectionError(msg)

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fora_do_ar)
    verificador = VerificadorDeToken(
        jwks_url="http://os.teste/.well-known/jwks.json",
        emissor=EMISSOR,
        audiencia=AUDIENCIA,
    )
    erro = falha(verificador, emitir_token("admin"))
    assert erro.status_code == 503


def test_jwks_fica_em_cache(
    monkeypatch: pytest.MonkeyPatch,
    jwks: dict[str, Any],
    emitir_token: Callable[..., str],
) -> None:
    chamadas: list[int] = []

    def buscar(_self: object) -> dict[str, Any]:
        chamadas.append(1)
        return jwks

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", buscar)
    verificador = VerificadorDeToken(
        jwks_url="http://os.teste/.well-known/jwks.json",
        emissor=EMISSOR,
        audiencia=AUDIENCIA,
    )
    for _ in range(3):
        verificador.verificar(emitir_token("admin"))
    assert chamadas == [1]
