"""API do orcamento: consulta/decisao interna e link publico do cliente."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.orcamento.aplicacao.dtos import ItemSolicitado
from src.orcamento.aplicacao.use_cases import GerarOrcamento
from src.orcamento.dominio.orcamento import TipoItem
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.orcamento.infraestrutura.tabela_de_precos import TabelaDePrecosMongoAdapter
from src.seed import semear
from tests.conftest import SUB_DO_TESTE
from tests.integracao.apoio import (
    URL_PUBLICA,
    RelogioFixo,
    eventos_do_outbox,
    token_do_link,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.orcamento.aplicacao.dtos import OrcamentoDTO

    Cabecalhos = Callable[[str], dict[str, str]]


PREFIXO = "/api/v1/publico/orcamentos"
ID_FIXO = UUID("0e3e4a2b-3c0a-4f5e-8d6e-1b2c3d4e5f60")
ERRO_DO_LINK = {
    "codigo": "LINK_DECISAO_INVALIDO",
    "mensagem": "Link de decisao invalido, expirado ou ja utilizado",
    "id_requisicao": None,
}


def gerar(app: FastAPI, *, ha: timedelta = timedelta(0)) -> tuple[OrcamentoDTO, str]:
    """Gera um orcamento como o consumidor de comandos fara; devolve o token."""
    banco = app.state.banco
    semear(banco)
    uow = MongoUnitOfWork(banco)
    dto = GerarOrcamento(
        uow,
        MongoOrcamentoRepository(uow),
        TabelaDePrecosMongoAdapter(uow),
        app.state.link_decisao,
        app.state.config.orcamento_validade,
        RelogioFixo(agora_utc() - ha),
    ).executar(
        ordem_id=uuid4(),
        itens=[
            ItemSolicitado(TipoItem.SERVICO, "SRV-ALINHAMENTO", 1),
            ItemSolicitado(TipoItem.PECA, "PEC-VELA", 4),
        ],
    )
    assert dto is not None
    [envelope] = eventos_do_outbox(banco, "OrcamentoGerado")
    link: str = envelope["dados"]["link_decisao"]
    return dto, link.removeprefix(URL_PUBLICA)


class TestApiInterna:
    def test_consulta_por_id_e_por_ordem(
        self, api: TestClient, app: FastAPI, cabecalhos: Cabecalhos
    ) -> None:
        dto, _ = gerar(app)

        resposta = api.get(
            f"/api/v1/orcamentos/{dto.id}", headers=cabecalhos("atendente")
        )

        assert resposta.status_code == 200
        corpo = resposta.json()
        assert corpo["status"] == "PENDENTE"
        assert corpo["total"] == "292.00"
        assert corpo["moeda"] == "BRL"
        assert corpo["decisao"] is None
        assert corpo["linhas"] == [
            {
                "tipo": "servico",
                "codigo": "SRV-ALINHAMENTO",
                "descricao": "Alinhamento e balanceamento",
                "quantidade": 1,
                "preco_unitario": "180.00",
                "subtotal": "180.00",
            },
            {
                "tipo": "peca",
                "codigo": "PEC-VELA",
                "descricao": "Vela de ignicao",
                "quantidade": 4,
                "preco_unitario": "28.00",
                "subtotal": "112.00",
            },
        ]
        por_ordem = api.get(
            "/api/v1/orcamentos",
            params={"ordem_id": str(dto.ordem_id)},
            headers=cabecalhos("admin"),
        )
        assert por_ordem.json() == [corpo]
        vazio = api.get(
            "/api/v1/orcamentos",
            params={"ordem_id": str(uuid4())},
            headers=cabecalhos("admin"),
        )
        assert vazio.json() == []

    def test_documento_corrompido_e_500_e_nao_422(
        self, api: TestClient, app: FastAPI, cabecalhos: Cabecalhos
    ) -> None:
        dto, _ = gerar(app)
        app.state.banco["orcamentos"].update_one(
            {"_id": dto.id},
            {"$set": {"status": "APROVADO"}},
            bypass_document_validation=True,
        )
        resposta = api.get(
            f"/api/v1/orcamentos/{dto.id}", headers=cabecalhos("atendente")
        )
        assert resposta.status_code == 500
        assert resposta.json()["erro"]["codigo"] == "ERRO_INTERNO"

    def test_inexistente_da_404(self, api: TestClient, cabecalhos: Cabecalhos) -> None:
        resposta = api.get(f"/api/v1/orcamentos/{uuid4()}", headers=cabecalhos("admin"))
        assert resposta.status_code == 404
        assert resposta.json()["erro"]["codigo"] == "ENTIDADE_NAO_ENCONTRADA"

    def test_atendente_decide_em_nome_do_cliente(
        self, api: TestClient, app: FastAPI, cabecalhos: Cabecalhos
    ) -> None:
        dto, _ = gerar(app)
        caminho = f"/api/v1/orcamentos/{dto.id}/decisao"

        aprovado = api.post(
            caminho, json={"decisao": "aprovar"}, headers=cabecalhos("atendente")
        )

        assert aprovado.status_code == 200
        assert aprovado.json()["status"] == "APROVADO"
        assert aprovado.json()["decisao"]["canal"] == "atendente"
        assert aprovado.json()["decisao"]["decidido_por"] == SUB_DO_TESTE
        [envelope] = eventos_do_outbox(app.state.banco, "OrcamentoAprovado")
        assert envelope["dados"]["decidido_por"] == SUB_DO_TESTE
        de_novo = api.post(
            caminho, json={"decisao": "recusar"}, headers=cabecalhos("atendente")
        )
        assert de_novo.status_code == 409
        assert de_novo.json()["erro"]["codigo"] == "TRANSICAO_STATUS_INVALIDA"
        assert [e["tipo"] for e in eventos_do_outbox(app.state.banco)] == [
            "OrcamentoGerado",
            "OrcamentoAprovado",
        ]

    def test_decisao_depois_do_prazo_da_410(
        self, api: TestClient, app: FastAPI, cabecalhos: Cabecalhos
    ) -> None:
        dto, _ = gerar(app, ha=timedelta(hours=73))
        resposta = api.post(
            f"/api/v1/orcamentos/{dto.id}/decisao",
            json={"decisao": "aprovar"},
            headers=cabecalhos("atendente"),
        )
        assert resposta.status_code == 410
        assert resposta.json()["erro"]["codigo"] == "ORCAMENTO_VENCIDO"

    def test_decisao_invalida_da_422(
        self, api: TestClient, cabecalhos: Cabecalhos
    ) -> None:
        resposta = api.post(
            f"/api/v1/orcamentos/{uuid4()}/decisao",
            json={"decisao": "talvez"},
            headers=cabecalhos("atendente"),
        )
        assert resposta.status_code == 422


class TestLinkPublico:
    def test_cliente_consulta_e_aprova_pelo_link_sem_login(
        self, api: TestClient, app: FastAPI
    ) -> None:
        dto, caminho = gerar(app)

        consulta = api.get(caminho)
        assert consulta.status_code == 200
        corpo = consulta.json()
        assert set(corpo) == {
            "id",
            "status",
            "linhas",
            "total",
            "moeda",
            "valido_ate",
            "decisao",
        }
        assert corpo["id"] == str(dto.id)

        decisao = api.post(f"{caminho}/decisao", json={"decisao": "aprovar"})
        assert decisao.status_code == 200
        assert decisao.json()["status"] == "APROVADO"
        assert decisao.json()["decisao"]["canal"] == "link"
        [envelope] = eventos_do_outbox(app.state.banco, "OrcamentoAprovado")
        assert envelope["dados"]["canal"] == "link"

    def test_decisao_pelo_link_e_unica(self, api: TestClient, app: FastAPI) -> None:
        _, caminho = gerar(app)
        resposta = api.post(f"{caminho}/decisao", json={"decisao": "recusar"})
        assert resposta.json()["status"] == "RECUSADO"
        segunda = api.post(f"{caminho}/decisao", json={"decisao": "aprovar"})
        consulta = api.get(caminho)
        assert (segunda.status_code, consulta.status_code) == (404, 404)
        assert segunda.json()["erro"] | {"id_requisicao": None} == ERRO_DO_LINK
        assert consulta.json()["erro"] | {"id_requisicao": None} == ERRO_DO_LINK

    @pytest.mark.parametrize(
        "caso",
        [
            pytest.param("malformado", id="malformado"),
            pytest.param("assinatura-falsa", id="assinatura-falsa"),
            pytest.param("expirado", id="expirado"),
            pytest.param("inexistente", id="orcamento-inexistente"),
        ],
    )
    def test_link_invalido_e_sempre_o_mesmo_404(
        self, api: TestClient, app: FastAPI, caso: str
    ) -> None:
        caminhos = {
            "malformado": f"{PREFIXO}/nao-e-token",
            "assinatura-falsa": f"{PREFIXO}/{ID_FIXO}.4102444800.assinatura-falsa",
            "inexistente": f"{PREFIXO}/{token_do_link(ID_FIXO, agora_utc())}",
        }
        caminho = caminhos.get(caso) or gerar(app, ha=timedelta(hours=73))[1]
        consulta = api.get(caminho)
        decisao = api.post(f"{caminho}/decisao", json={"decisao": "aprovar"})
        for resposta in (consulta, decisao):
            assert resposta.status_code == 404
            assert resposta.json()["erro"] | {"id_requisicao": None} == ERRO_DO_LINK
