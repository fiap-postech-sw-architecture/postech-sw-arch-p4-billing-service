"""Boot da API e seed de demonstracao."""

from __future__ import annotations

import secrets
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from src import seed
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.main import criar_app
from src.precos.aplicacao.use_cases import PrecosDeServicos
from src.precos.infraestrutura.repository import MongoPrecoServicoRepository
from tests.integracao.apoio import configuracao

if TYPE_CHECKING:
    from pymongo import MongoClient
    from pymongo.database import Database

    Banco = Database[dict[str, Any]]


def test_saude_sem_autenticacao(api: TestClient) -> None:
    resposta = api.get("/api/v1/saude")
    assert resposta.status_code == 200
    assert resposta.json() == {"status": "ok", "modo": "simulado"}
    assert resposta.headers["X-Content-Type-Options"] == "nosniff"


def test_metrics_expoe_http_e_dependencias(api: TestClient) -> None:
    api.get("/api/v1/saude")
    texto = api.get("/metrics").text
    assert (
        'http_request_duration_seconds_count{method="GET",rota="/api/v1/saude"' in texto
    )
    assert "pytstop_circuit_breaker_aberto" in texto


def test_openapi_documenta_as_rotas_do_billing(api: TestClient) -> None:
    caminhos = set(api.get("/openapi.json").json()["paths"])
    assert {
        "/api/v1/precos/servicos",
        "/api/v1/precos/pecas/{sku}",
        "/api/v1/precos/validacao",
        "/api/v1/orcamentos",
        "/api/v1/orcamentos/{orcamento_id}/decisao",
        "/api/v1/publico/orcamentos/{token}/decisao",
        "/api/v1/pagamentos/{pagamento_id}",
        "/api/v1/webhooks/mercadopago",
        "/simulador/checkout/{pagamento_id}",
        "/api/v1/saude",
    } <= caminhos


def test_boot_conecta_no_mongodb_pela_configuracao(
    mongo_uri: str,
    cliente_mongo: MongoClient[dict[str, Any]],
    jwks_publicado: dict[str, Any],
) -> None:
    nome = f"teste_{uuid4().hex}"
    app = criar_app(configuracao(MONGODB_URI=mongo_uri, MONGODB_DB=nome))
    try:
        with TestClient(app) as cliente:
            assert cliente.get("/api/v1/saude").status_code == 200
        assert "outbox" in cliente_mongo[nome].list_collection_names()
    finally:
        cliente_mongo.drop_database(nome)


PRODUCAO = {
    "ENVIRONMENT": "production",
    "ORCAMENTO_LINK_SECRET": secrets.token_hex(32),
    "MONGODB_URI": "mongodb://nao-usado-com-banco-injetado:27017",
}


def test_producao_recusa_subir_com_o_simulador_sem_permissao() -> None:
    with pytest.raises(ValueError, match="MP_MODE=simulado recusado"):
        configuracao(**PRODUCAO)


def test_simulador_permitido_em_producao_sobe_e_fica_no_log(
    banco: Banco, capsys: pytest.CaptureFixture[str]
) -> None:
    config = configuracao(**PRODUCAO, SIMULADOR_PERMITIDO="true")
    app = criar_app(config, banco=banco)
    with TestClient(app) as cliente:
        assert cliente.get("/api/v1/saude").json()["modo"] == "simulado"
    assert "payment_simulator_enabled_in_production" in capsys.readouterr().out


class TestSeed:
    def test_seed_e_idempotente_e_nao_sobrescreve_o_admin(self, banco: Banco) -> None:
        assert seed.semear(banco) == (11, 0)
        uow = MongoUnitOfWork(banco)
        servicos = PrecosDeServicos(uow, MongoPrecoServicoRepository(uow))
        servicos.atualizar(
            "SRV-FREIOS",
            nome="Revisao do sistema de freios",
            descricao="Ajustado pelo admin",
            preco=Decimal("275.00"),
            ativo=True,
        )

        assert seed.semear(banco) == (0, 11)

        assert servicos.obter("SRV-FREIOS").preco == Decimal("275.00")
        assert banco["precos_servicos"].count_documents({}) == 5
        assert banco["precos_pecas"].count_documents({}) == 6

    def test_tabela_da_demonstracao(self, banco: Banco) -> None:
        seed.semear(banco)
        uow = MongoUnitOfWork(banco)
        servicos = PrecosDeServicos(uow, MongoPrecoServicoRepository(uow))
        assert {
            s.codigo: str(s.preco) for s in servicos.listar(offset=0, limit=10).itens
        } == {
            "SRV-TROCA-OLEO": "120.00",
            "SRV-ALINHAMENTO": "180.00",
            "SRV-FREIOS": "250.00",
            "SRV-DIAGNOSTICO": "150.00",
            "SRV-SUSPENSAO": "320.00",
        }
        pecas = {
            d["sku"]: d["preco"]["valor"].to_decimal()
            for d in banco["precos_pecas"].find()
        }
        assert pecas == {
            "PEC-OLEO-5W30": Decimal("45.00"),
            "PEC-FILTRO-OLEO": Decimal("35.00"),
            "PEC-PASTILHA-FREIO": Decimal("160.00"),
            "PEC-DISCO-FREIO": Decimal("220.00"),
            "PEC-AMORTECEDOR": Decimal("390.00"),
            "PEC-VELA": Decimal("28.00"),
        }

    def test_comando_do_seed(
        self,
        mongo_uri: str,
        cliente_mongo: MongoClient[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        nome = f"teste_{uuid4().hex}"
        # So o banco: o seed nao exige segredo, JWKS nem modo de pagamento.
        monkeypatch.setenv("ENVIRONMENT", "test")
        monkeypatch.setenv("MONGODB_URI", mongo_uri)
        monkeypatch.setenv("MONGODB_DB", nome)
        try:
            seed.main()
            seed.main()
        finally:
            cliente_mongo.drop_database(nome)
        assert capsys.readouterr().out.splitlines() == [
            "seed de precos: 11 criados, 0 ja existiam",
            "seed de precos: 0 criados, 11 ja existiam",
        ]
