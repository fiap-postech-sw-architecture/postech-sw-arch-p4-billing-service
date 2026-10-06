from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import timedelta
from uuid import uuid4

import pytest

from src.orcamento.aplicacao.link_decisao import CAMINHO_PUBLICO, LinkDeDecisao
from src.orcamento.dominio.exceptions import (
    LinkDeDecisaoExpiradoError,
    LinkDeDecisaoInvalidoError,
)
from tests.factories import AGORA

SEGREDO = "segredo-de-teste-com-mais-de-32-bytes!!"
EXPIRA_EM = AGORA + timedelta(hours=72)


@pytest.fixture
def link() -> LinkDeDecisao:
    return LinkDeDecisao(segredo=SEGREDO, url_base="http://billing.teste/")


def test_url_completa_aponta_para_a_rota_publica(link: LinkDeDecisao) -> None:
    orcamento_id = uuid4()
    url = link.gerar(orcamento_id, EXPIRA_EM)
    assert url == (
        f"http://billing.teste{CAMINHO_PUBLICO}/{link.token(orcamento_id, EXPIRA_EM)}"
    )


def test_token_e_hmac_sha256_sobre_id_e_expiracao(link: LinkDeDecisao) -> None:
    orcamento_id = uuid4()
    corpo = f"{orcamento_id}.{int(EXPIRA_EM.timestamp())}"
    assinatura = hmac.new(SEGREDO.encode(), corpo.encode(), hashlib.sha256).digest()
    esperado = base64.urlsafe_b64encode(assinatura).rstrip(b"=").decode()
    assert link.token(orcamento_id, EXPIRA_EM) == f"{corpo}.{esperado}"


def test_token_valido_devolve_o_orcamento_ate_a_expiracao(link: LinkDeDecisao) -> None:
    orcamento_id = uuid4()
    token = link.token(orcamento_id, EXPIRA_EM)
    assert link.validar(token, agora=AGORA) == orcamento_id
    assert link.validar(token, agora=EXPIRA_EM) == orcamento_id


def test_token_expirado(link: LinkDeDecisao) -> None:
    emitido = link.token(uuid4(), EXPIRA_EM)
    with pytest.raises(LinkDeDecisaoExpiradoError):
        link.validar(emitido, agora=EXPIRA_EM + timedelta(seconds=1))


def test_expiracao_adulterada_invalida_a_assinatura(link: LinkDeDecisao) -> None:
    orcamento_id, _, assinatura = link.token(uuid4(), EXPIRA_EM).split(".")
    adulterado = f"{orcamento_id}.{int(EXPIRA_EM.timestamp()) + 86400}.{assinatura}"
    with pytest.raises(LinkDeDecisaoInvalidoError):
        link.validar(adulterado, agora=AGORA)


def test_outro_orcamento_com_a_mesma_assinatura_e_invalido(link: LinkDeDecisao) -> None:
    _, expiracao, assinatura = link.token(uuid4(), EXPIRA_EM).split(".")
    with pytest.raises(LinkDeDecisaoInvalidoError):
        link.validar(f"{uuid4()}.{expiracao}.{assinatura}", agora=AGORA)


def test_token_de_outro_segredo_e_invalido(link: LinkDeDecisao) -> None:
    outro = LinkDeDecisao(segredo="outro-segredo-qualquer", url_base="http://x")
    with pytest.raises(LinkDeDecisaoInvalidoError):
        link.validar(outro.token(uuid4(), EXPIRA_EM), agora=AGORA)


@pytest.mark.parametrize(
    "token", ["", "abc", "a.b", "a.b.c.d", "ação.123.çã", "nao-uuid.123.xyz"]
)
def test_token_malformado_e_invalido(link: LinkDeDecisao, token: str) -> None:
    with pytest.raises(LinkDeDecisaoInvalidoError):
        link.validar(token, agora=AGORA)


def test_conteudo_assinado_mas_nao_parseavel_e_invalido() -> None:
    # Assinatura valida sobre id/expiracao que nao sao UUID/inteiro: so quem
    # tem o segredo produz isso, mas o parse nao pode estourar 500.
    link = LinkDeDecisao(segredo=SEGREDO, url_base="http://x")
    corpo = "nao-e-uuid.amanha"
    token = f"{corpo}.{link._assinar(corpo)}"
    with pytest.raises(LinkDeDecisaoInvalidoError):
        link.validar(token, agora=AGORA)


def test_segredo_vazio_e_recusado() -> None:
    with pytest.raises(ValueError, match="Segredo"):
        LinkDeDecisao(segredo="", url_base="http://x")
