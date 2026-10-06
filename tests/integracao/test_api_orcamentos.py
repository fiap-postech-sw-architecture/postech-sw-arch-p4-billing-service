"""API do orcamento: consulta/decisao interna e link publico do cliente."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.orcamento.aplicacao.dtos import ItemSolicitado
from src.orcamento.aplicacao.use_cases import GerarOrcamento
from src.orcamento.dominio.orcamento import TipoItem
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.orcamento.infraestrutura.tabela_de_precos import TabelaDePrecosMongoAdapter
from src.seed import semear
from tests.integracao.apoio import URL_PUBLICA, RelogioFixo, eventos_do_outbox

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.orcamento.aplicacao.dtos import OrcamentoDTO

    Cabecalhos = Callable[[str], dict[str, str]]


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

    def test_inexistente_da_404(self, api: TestClient, cabecalhos: Cabecalhos) -> None:
        resposta = api.get(f"/api/v1/orcamentos/{uuid4()}", headers=cabecalhos("admin"))
        assert resposta.status_code == 404
        assert resposta.json()["erro"]["codigo"] == "ORCAMENTO_NAO_ENCONTRADO"

    def test_mecanico_nao_consulta_orcamento(
        self, api: TestClient, cabecalhos: Cabecalhos
    ) -> None:
        resposta = api.get(
            f"/api/v1/orcamentos/{uuid4()}", headers=cabecalhos("mecanico")
        )
        assert resposta.status_code == 403

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

    def test_cliente_recusa_pelo_link(self, api: TestClient, app: FastAPI) -> None:
        _, caminho = gerar(app)
        resposta = api.post(f"{caminho}/decisao", json={"decisao": "recusar"})
        assert resposta.json()["status"] == "RECUSADO"
        segunda = api.post(f"{caminho}/decisao", json={"decisao": "aprovar"})
        assert segunda.status_code == 409

    @pytest.mark.parametrize(
        "token", ["nao-e-token", f"{uuid4()}.4102444800.assinatura-falsa"]
    )
    def test_link_adulterado_da_404(self, api: TestClient, token: str) -> None:
        resposta = api.get(f"/api/v1/publico/orcamentos/{token}")
        assert resposta.status_code == 404
        assert resposta.json()["erro"]["codigo"] == "LINK_DECISAO_INVALIDO"
        decisao = api.post(
            f"/api/v1/publico/orcamentos/{token}/decisao", json={"decisao": "aprovar"}
        )
        assert decisao.status_code == 404

    def test_link_expirado_da_410(self, api: TestClient, app: FastAPI) -> None:
        _, caminho = gerar(app, ha=timedelta(hours=73))
        resposta = api.get(caminho)
        assert resposta.status_code == 410
        assert resposta.json()["erro"]["codigo"] == "LINK_DECISAO_EXPIRADO"


def test_rotas_internas_exigem_token(api: TestClient) -> None:
    resposta: Any = api.get(f"/api/v1/orcamentos/{uuid4()}")
    assert resposta.status_code == 401
