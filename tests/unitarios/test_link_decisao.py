from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from src.compartilhado.aplicacao.token_assinado import TokenAssinado
from src.orcamento.aplicacao.link_decisao import LinkDeDecisao
from src.orcamento.dominio.exceptions import LinkDeDecisaoInvalidoError
from src.pagamento.infraestrutura.simulado import GatewayPagamentoSimulado
from tests.factories import AGORA

SEGREDO = "segredo-de-teste-com-mais-de-32-bytes!!"
EXPIRA_EM = AGORA + timedelta(hours=72)
BASE = "http://billing.teste/api/v1/publico/orcamentos"


@pytest.fixture
def link() -> LinkDeDecisao:
    return LinkDeDecisao(segredo=SEGREDO, url_base=f"{BASE}/")


def test_url_e_a_base_com_o_token(link: LinkDeDecisao) -> None:
    orcamento_id = uuid4()
    url = link.gerar(orcamento_id, EXPIRA_EM)
    assert url.startswith(f"{BASE}/{orcamento_id}.{int(EXPIRA_EM.timestamp())}.")
    assert link.validar(url.removeprefix(f"{BASE}/"), agora=AGORA) == orcamento_id


def test_vale_ate_a_validade_do_orcamento(link: LinkDeDecisao) -> None:
    orcamento_id = uuid4()
    emitido = link.gerar(orcamento_id, EXPIRA_EM).removeprefix(f"{BASE}/")
    assert link.validar(emitido, agora=EXPIRA_EM) == orcamento_id
    with pytest.raises(LinkDeDecisaoInvalidoError):
        link.validar(emitido, agora=EXPIRA_EM + timedelta(seconds=1))


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("", id="vazio"),
        pytest.param("nao-e-token", id="malformado"),
        pytest.param(f"{uuid4()}.4102444800.assinatura-falsa", id="assinatura-falsa"),
    ],
)
def test_token_invalido_e_o_mesmo_erro(link: LinkDeDecisao, token: str) -> None:
    with pytest.raises(LinkDeDecisaoInvalidoError):
        link.validar(token, agora=AGORA)


def test_token_de_outro_uso_do_mesmo_segredo_nao_vale(link: LinkDeDecisao) -> None:
    """O token do checkout simulado (mesmo segredo) nao decide orcamento."""
    pagamento_id = uuid4()
    simulador = GatewayPagamentoSimulado(url_checkout="http://x", segredo=SEGREDO)
    cobranca = simulador.criar_cobranca(
        pagamento_id=pagamento_id, itens=[], expira_em=EXPIRA_EM
    )
    do_checkout = cobranca.checkout_url.split("token=")[1]
    with pytest.raises(LinkDeDecisaoInvalidoError):
        link.validar(do_checkout, agora=AGORA)
    sem_dominio = TokenAssinado(segredo=SEGREDO, dominio="outro")
    with pytest.raises(LinkDeDecisaoInvalidoError):
        link.validar(sem_dominio.emitir(pagamento_id, EXPIRA_EM), agora=AGORA)


def test_segredo_vazio_e_recusado() -> None:
    with pytest.raises(ValueError, match="Segredo"):
        LinkDeDecisao(segredo="", url_base="http://x")
