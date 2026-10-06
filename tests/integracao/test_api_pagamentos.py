"""API do pagamento: consulta, webhook do Mercado Pago e simulador."""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
import respx
from fastapi.testclient import TestClient

from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.main import criar_app
from src.orcamento.dominio.orcamento import CanalDecisao
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.pagamento.aplicacao.use_cases import SolicitarPagamento
from src.pagamento.infraestrutura.orcamentos import OrcamentosMongoAdapter
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository
from tests.factories import dinheiro, orcamento, pagamento
from tests.integracao.apoio import (
    SEGREDO_WEBHOOK,
    URL_PUBLICA,
    configuracao,
    eventos_do_outbox,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fastapi import FastAPI
    from pymongo.database import Database

    from src.pagamento.aplicacao.dtos import PagamentoDTO

    Cabecalhos = Callable[[str], dict[str, str]]
    Banco = Database[dict[str, Any]]

WEBHOOK = "/api/v1/webhooks/mercadopago"


def assinatura(data_id: str, request_id: str = "req-mp-1") -> dict[str, str]:
    ts = "1742505638683"
    manifesto = f"id:{data_id.lower()};request-id:{request_id};ts:{ts};"
    v1 = hmac.new(
        SEGREDO_WEBHOOK.encode(), manifesto.encode(), hashlib.sha256
    ).hexdigest()
    return {"x-signature": f"ts={ts},v1={v1}", "x-request-id": request_id}


def notificacao(data_id: str) -> dict[str, Any]:
    """Corpo do webhook no formato da documentacao do Mercado Pago."""
    return {
        "action": "payment.updated",
        "api_version": "v1",
        "data": {"id": data_id},
        "date_created": "2021-11-01T02:02:02Z",
        "id": "123456",
        "live_mode": False,
        "type": "payment",
        "user_id": 724484980,
    }


def solicitado(app: FastAPI) -> PagamentoDTO:
    banco = app.state.banco
    aprovado = orcamento()
    aprovado.aprovar(canal=CanalDecisao.LINK, agora=aprovado.criado_em)
    uow = MongoUnitOfWork(banco)
    uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(aprovado))
    uow = MongoUnitOfWork(banco)
    return SolicitarPagamento(
        uow,
        MongoPagamentoRepository(uow),
        OrcamentosMongoAdapter(uow),
        app.state.gateway_pagamento,
        app.state.config.pagamento_validade,
    ).executar(ordem_id=aprovado.ordem_id, orcamento_id=aprovado.id)


class TestConsulta:
    def test_atendente_consulta_pagamento(
        self, api: TestClient, app: FastAPI, cabecalhos: Cabecalhos
    ) -> None:
        dto = solicitado(app)
        resposta = api.get(
            f"/api/v1/pagamentos/{dto.id}", headers=cabecalhos("atendente")
        )
        assert resposta.status_code == 200
        corpo = resposta.json()
        assert (corpo["status"], corpo["valor"], corpo["moeda"]) == (
            "PENDENTE",
            "335.00",
            "BRL",
        )
        assert corpo["checkout_url"] == f"{URL_PUBLICA}/simulador/checkout/{dto.id}"
        assert corpo["notificacoes"] == []

    def test_inexistente_e_permissao(
        self, api: TestClient, cabecalhos: Cabecalhos
    ) -> None:
        caminho = f"/api/v1/pagamentos/{uuid4()}"
        assert api.get(caminho, headers=cabecalhos("admin")).status_code == 404
        assert api.get(caminho, headers=cabecalhos("mecanico")).status_code == 403


class TestWebhook:
    def test_notificacao_assinada_confirma_pelo_provedor(
        self, api: TestClient, app: FastAPI
    ) -> None:
        dto = solicitado(app)
        referencia = app.state.gateway_pagamento.registrar_resultado(
            pagamento_id=dto.id, valor=dinheiro("335.00"), aprovado=True
        )

        resposta = api.post(
            WEBHOOK,
            params={"data.id": referencia, "type": "payment"},
            json=notificacao(referencia),
            headers=assinatura(referencia),
        )

        assert resposta.status_code == 200
        assert resposta.json() == {"processado": True}
        [envelope] = eventos_do_outbox(app.state.banco, "PagamentoConfirmado")
        assert envelope["dados"]["pagamento_id"] == str(dto.id)

    def test_data_id_do_corpo_quando_a_url_nao_traz(
        self, api: TestClient, app: FastAPI
    ) -> None:
        dto = solicitado(app)
        referencia = app.state.gateway_pagamento.registrar_resultado(
            pagamento_id=dto.id, valor=dinheiro("335.00"), aprovado=False
        )
        resposta = api.post(
            WEBHOOK, json=notificacao(referencia), headers=assinatura(referencia)
        )
        assert resposta.json() == {"processado": True}
        assert len(eventos_do_outbox(app.state.banco, "PagamentoRecusado")) == 1

    @pytest.mark.parametrize(
        "cabecalhos_do_mp",
        [{}, {"x-signature": "ts=1,v1=00", "x-request-id": "req-mp-1"}],
    )
    def test_assinatura_ausente_ou_invalida_da_401(
        self, api: TestClient, app: FastAPI, cabecalhos_do_mp: dict[str, str]
    ) -> None:
        resposta = api.post(
            WEBHOOK,
            params={"data.id": "123", "type": "payment"},
            json=notificacao("123"),
            headers=cabecalhos_do_mp,
        )
        assert resposta.status_code == 401
        assert resposta.json()["erro"]["codigo"] == "NAO_AUTENTICADO"
        assert eventos_do_outbox(app.state.banco) == []

    def test_assinatura_de_outro_pagamento_nao_vale(self, api: TestClient) -> None:
        resposta = api.post(
            WEBHOOK,
            params={"data.id": "999", "type": "payment"},
            json=notificacao("999"),
            headers=assinatura("123"),
        )
        assert resposta.status_code == 401

    def test_pagamento_que_nao_e_nosso_e_reconhecido_sem_processar(
        self, api: TestClient
    ) -> None:
        resposta = api.post(
            WEBHOOK,
            params={"data.id": "sim-desconhecido", "type": "payment"},
            json=notificacao("sim-desconhecido"),
            headers=assinatura("sim-desconhecido"),
        )
        assert resposta.status_code == 200
        assert resposta.json() == {"processado": False}

    def test_outro_tipo_de_notificacao_e_so_reconhecido(self, api: TestClient) -> None:
        corpo = {**notificacao("42"), "type": "merchant_order"}
        resposta = api.post(
            WEBHOOK,
            params={"data.id": "42", "type": "merchant_order"},
            json=corpo,
            headers=assinatura("42"),
        )
        assert resposta.json() == {"processado": False}

    def test_sem_segredo_configurado_o_webhook_fica_desligado(
        self, banco: Banco, jwks_publicado: dict[str, Any]
    ) -> None:
        app = criar_app(configuracao(MP_WEBHOOK_SECRET=""), banco=banco)
        with TestClient(app) as cliente:
            resposta = cliente.post(
                WEBHOOK, json=notificacao("1"), headers=assinatura("1")
            )
        assert resposta.status_code == 503


class TestSimulador:
    def test_checkout_html_com_botoes(self, api: TestClient, app: FastAPI) -> None:
        dto = solicitado(app)
        resposta = api.get(f"/simulador/checkout/{dto.id}")
        assert resposta.status_code == 200
        assert resposta.headers["content-type"].startswith("text/html")
        base = f"{URL_PUBLICA}/api/v1/simulador/pagamentos/{dto.id}"
        assert f'action="{base}/aprovar"' in resposta.text
        assert f'action="{base}/recusar"' in resposta.text
        assert "BRL 335.00" in resposta.text

    def test_aprovar_e_recusar(self, api: TestClient, app: FastAPI) -> None:
        aprovado = solicitado(app)
        resposta = api.post(f"/api/v1/simulador/pagamentos/{aprovado.id}/aprovar")
        assert resposta.status_code == 200
        assert resposta.json()["status"] == "APROVADO"
        assert resposta.json()["notificacoes"][0]["status_provedor"] == "approved"
        de_novo = api.post(f"/api/v1/simulador/pagamentos/{aprovado.id}/recusar")
        assert de_novo.status_code == 409

        recusado = solicitado(app)
        resposta = api.post(f"/api/v1/simulador/pagamentos/{recusado.id}/recusar")
        assert resposta.json()["status"] == "RECUSADO"

    def test_pagamento_inexistente(self, api: TestClient) -> None:
        assert api.get(f"/simulador/checkout/{uuid4()}").status_code == 404
        resposta = api.post(f"/api/v1/simulador/pagamentos/{uuid4()}/aprovar")
        assert resposta.status_code == 404


class TestModoMercadoPago:
    @pytest.fixture
    def api_mp(
        self, banco: Banco, jwks_publicado: dict[str, Any]
    ) -> Iterator[TestClient]:
        app = criar_app(
            configuracao(
                MP_MODE="mercadopago",
                MP_ACCESS_TOKEN="TEST-token-de-teste",  # gitleaks:allow
            ),
            banco=banco,
        )
        with TestClient(app) as cliente:
            yield cliente

    def test_simulador_nao_existe(self, api_mp: TestClient) -> None:
        pagamento_id = uuid4()
        assert api_mp.get(f"/simulador/checkout/{pagamento_id}").status_code == 404
        resposta = api_mp.post(f"/api/v1/simulador/pagamentos/{pagamento_id}/aprovar")
        assert resposta.status_code == 404

    def test_webhook_consulta_o_mercado_pago_de_verdade(
        self, api_mp: TestClient, banco: Banco
    ) -> None:
        pendente = pagamento()
        uow = MongoUnitOfWork(banco)
        uow.executar(lambda: MongoPagamentoRepository(uow).salvar(pendente))
        with respx.mock(base_url="https://api.mercadopago.com") as mp:
            consulta = mp.get("/v1/payments/1234567890").respond(
                200,
                json={
                    "id": 1234567890,
                    "status": "approved",
                    "status_detail": "accredited",
                    "external_reference": str(pendente.id),
                    "transaction_amount": 335.0,
                    "currency_id": "BRL",
                },
            )
            resposta = api_mp.post(
                WEBHOOK,
                params={"data.id": "1234567890", "type": "payment"},
                json=notificacao("1234567890"),
                headers=assinatura("1234567890"),
            )

        assert resposta.json() == {"processado": True}
        autorizacao = consulta.calls.last.request.headers["Authorization"]
        assert autorizacao == "Bearer TEST-token-de-teste"
        [envelope] = eventos_do_outbox(banco, "PagamentoConfirmado")
        assert envelope["dados"]["referencia_provedor"] == "1234567890"

    def test_mercado_pago_fora_do_ar_da_503_para_o_provedor_reenviar(
        self, api_mp: TestClient
    ) -> None:
        with respx.mock(base_url="https://api.mercadopago.com") as mp:
            mp.get("/v1/payments/1").respond(503)
            resposta = api_mp.post(
                WEBHOOK,
                params={"data.id": "1", "type": "payment"},
                json=notificacao("1"),
                headers=assinatura("1"),
            )
        assert resposta.status_code == 503
        assert resposta.json()["erro"]["codigo"] == "GATEWAY_PAGAMENTO_INDISPONIVEL"
