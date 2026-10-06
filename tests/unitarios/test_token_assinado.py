from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import timedelta
from uuid import uuid4

import pytest

from src.compartilhado.aplicacao.token_assinado import TokenAssinado
from tests.factories import AGORA

SEGREDO = "segredo-de-teste-com-mais-de-32-bytes!!"
EXPIRA_EM = AGORA + timedelta(hours=72)


@pytest.fixture
def assinador() -> TokenAssinado:
    return TokenAssinado(segredo=SEGREDO, dominio="teste")


def assinar(corpo: str, dominio: str = "teste") -> str:
    digest = hmac.new(
        SEGREDO.encode(), f"{dominio}:{corpo}".encode(), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_token_e_hmac_sha256_do_dominio_id_e_expiracao(
    assinador: TokenAssinado,
) -> None:
    recurso = uuid4()
    corpo = f"{recurso}.{int(EXPIRA_EM.timestamp())}"
    assert assinador.emitir(recurso, EXPIRA_EM) == f"{corpo}.{assinar(corpo)}"


def test_vale_ate_a_expiracao_inclusive(assinador: TokenAssinado) -> None:
    recurso = uuid4()
    emitido = assinador.emitir(recurso, EXPIRA_EM)
    depois = EXPIRA_EM + timedelta(seconds=1)
    assert assinador.validar(emitido, agora=AGORA) == recurso
    assert assinador.validar(emitido, agora=EXPIRA_EM) == recurso
    assert assinador.validar(emitido, agora=depois) is None


def test_expiracao_fracionaria_arredonda_para_o_segundo_acima(
    assinador: TokenAssinado,
) -> None:
    recurso = uuid4()
    instante = AGORA + timedelta(milliseconds=400)
    token = assinador.emitir(recurso, instante)
    assert token.split(".")[1] == str(int(AGORA.timestamp()) + 1)
    assert assinador.validar(token, agora=instante) == recurso


def test_dominios_diferentes_nao_aceitam_o_token_um_do_outro(
    assinador: TokenAssinado,
) -> None:
    outro = TokenAssinado(segredo=SEGREDO, dominio="outro-uso")
    recurso = uuid4()
    assert outro.validar(assinador.emitir(recurso, EXPIRA_EM), agora=AGORA) is None
    assert assinador.validar(outro.emitir(recurso, EXPIRA_EM), agora=AGORA) is None


def test_segredo_diferente_nao_aceita(assinador: TokenAssinado) -> None:
    outro = TokenAssinado(segredo="outro-segredo-qualquer", dominio="teste")
    assert assinador.validar(outro.emitir(uuid4(), EXPIRA_EM), agora=AGORA) is None


def test_expiracao_ou_id_adulterados_invalidam(assinador: TokenAssinado) -> None:
    recurso, expiracao, assinatura = assinador.emitir(uuid4(), EXPIRA_EM).split(".")
    estendido = f"{recurso}.{int(expiracao) + 86400}.{assinatura}"
    trocado = f"{uuid4()}.{expiracao}.{assinatura}"
    assert assinador.validar(estendido, agora=AGORA) is None
    assert assinador.validar(trocado, agora=AGORA) is None


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("", id="vazio"),
        pytest.param("abc", id="uma-parte"),
        pytest.param("a.b", id="duas-partes"),
        pytest.param("a.b.c.d", id="quatro-partes"),
        pytest.param("ação.123.çã", id="nao-ascii"),
        pytest.param("nao-uuid.123.xyz", id="assinatura-errada"),
    ],
)
def test_token_malformado_e_invalido(assinador: TokenAssinado, token: str) -> None:
    assert assinador.validar(token, agora=AGORA) is None


def test_conteudo_assinado_mas_nao_parseavel_e_invalido(
    assinador: TokenAssinado,
) -> None:
    # Assinatura valida sobre id/expiracao que nao sao UUID/inteiro: so quem
    # tem o segredo produz isso, mas o parse nao pode estourar 500.
    corpo = "nao-e-uuid.amanha"
    token = f"{corpo}.{assinar(corpo)}"
    assert assinador.validar(token, agora=AGORA) is None


def test_segredo_vazio_e_recusado() -> None:
    with pytest.raises(ValueError, match="Segredo"):
        TokenAssinado(segredo="", dominio="teste")
