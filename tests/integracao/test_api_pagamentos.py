"""API do pagamento: consulta, webhook do Mercado Pago e simulador."""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
import respx
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.main import criar_app
from src.orcamento.dominio.orcamento import CanalDecisao
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.pagamento.aplicacao.use_cases import EstornarPagamento, SolicitarPagamento
from src.pagamento.dominio.cobranca import SituacaoNoProvedor
from src.pagamento.dominio.estados import StatusNoProvedor
from src.pagamento.infraestrutura.orcamentos import OrcamentosMongoAdapter
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository
from tests.factories import dinheiro, orcamento, pagamento
from tests.integracao.apoio import (
    SEGREDO_WEBHOOK,
    URL_PUBLICA,
    RelogioFixo,
    configuracao,
    eventos_do_outbox,
    token_do_checkout,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fastapi import FastAPI
    from pymongo.database import Database

    from src.compartilhado.dominio.relogio import Relogio
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


def solicitado(app: FastAPI, relogio: Relogio = agora_utc) -> PagamentoDTO:
    banco = app.state.banco
    aprovado = orcamento(criado_em=relogio())
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
        relogio,
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
        assert (corpo["status"], corpo["valor"], corpo["moeda"], corpo["recusas"]) == (
            "SOLICITADO",
            "335.00",
            "BRL",
            0,
        )
        assert corpo["checkout_url"] == dto.checkout_url
        assert dto.checkout_url is not None
        assert dto.checkout_url.startswith(
            f"{URL_PUBLICA}/simulador/checkout/{dto.id}?token="
        )
        assert corpo["notificacoes"] == []

    def test_inexistente_e_permissao(
        self, api: TestClient, cabecalhos: Cabecalhos
    ) -> None:
        caminho = f"/api/v1/pagamentos/{uuid4()}"
        assert api.get(caminho, headers=cabecalhos("admin")).status_code == 404
        assert api.get(caminho, headers=cabecalhos("mecanico")).status_code == 403


@pytest.fixture
def consultas(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Referencias consultadas no provedor durante o teste."""
    gateway = app.state.gateway_pagamento
    original = gateway.consultar_pagamento
    feitas: list[str] = []

    def espiar(referencia: str) -> object:
        feitas.append(referencia)
        return original(referencia)

    monkeypatch.setattr(gateway, "consultar_pagamento", espiar)
    return feitas


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

    @pytest.mark.parametrize(
        "com_data_no_corpo", [True, False], ids=["data", "sem-data"]
    )
    def test_sem_data_id_na_query_responde_200_sem_processar(
        self, api: TestClient, app: FastAPI, com_data_no_corpo: bool
    ) -> None:
        # O id do corpo nao e assinado (a x-signature cobre o data.id da query):
        # nada e consultado, mesmo com a assinatura certa para o id do corpo.
        dto = solicitado(app)
        referencia = app.state.gateway_pagamento.registrar_resultado(
            pagamento_id=dto.id, valor=dinheiro("335.00"), aprovado=False
        )
        corpo = notificacao(referencia)
        if not com_data_no_corpo:
            del corpo["data"]
        resposta = api.post(WEBHOOK, json=corpo, headers=assinatura(referencia))
        assert resposta.status_code == 200
        assert resposta.json() == {"processado": False}
        documento = app.state.banco["pagamentos"].find_one({"_id": dto.id})
        assert documento is not None
        assert documento["recusas"] == 0

    def test_id_consultado_e_o_da_query_e_nunca_o_do_corpo(
        self, api: TestClient, app: FastAPI, consultas: list[str]
    ) -> None:
        resposta = api.post(
            WEBHOOK,
            params={"data.id": "111", "type": "payment"},
            json=notificacao("222"),
            headers=assinatura("111"),
        )
        assert resposta.status_code == 200
        assert consultas == ["111"]

    def test_outro_tipo_nem_consulta_o_provedor(
        self, api: TestClient, consultas: list[str]
    ) -> None:
        resposta = api.post(
            WEBHOOK,
            params={"data.id": "42", "type": "merchant_order"},
            json={**notificacao("42"), "type": "merchant_order"},
            headers=assinatura("42"),
        )
        assert resposta.json() == {"processado": False}
        assert consultas == []

    def test_mesma_notificacao_repetida_confirma_uma_vez(
        self, api: TestClient, app: FastAPI
    ) -> None:
        dto = solicitado(app)
        referencia = app.state.gateway_pagamento.registrar_resultado(
            pagamento_id=dto.id, valor=dinheiro("335.00"), aprovado=True
        )
        for _ in range(2):
            resposta = api.post(
                WEBHOOK,
                params={"data.id": referencia, "type": "payment"},
                json=notificacao(referencia),
                headers=assinatura(referencia),
            )
            assert resposta.json() == {"processado": True}
        assert len(eventos_do_outbox(app.state.banco, "PagamentoConfirmado")) == 1

    def test_status_vem_da_consulta_e_nao_do_corpo(
        self, api: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dto = solicitado(app)
        pendente_no_provedor = SituacaoNoProvedor(
            referencia="777",
            referencia_externa=str(dto.id),
            status=StatusNoProvedor.EM_ANDAMENTO,
            status_provedor="pending",
            detalhe=None,
            valor=dinheiro("335.00"),
        )
        monkeypatch.setattr(
            app.state.gateway_pagamento,
            "consultar_pagamento",
            lambda _referencia: pendente_no_provedor,
        )
        corpo = {
            **notificacao("777"),
            "action": "payment.updated",
            "status": "approved",
        }
        resposta = api.post(
            WEBHOOK,
            params={"data.id": "777", "type": "payment"},
            json=corpo,
            headers=assinatura("777"),
        )
        assert resposta.json() == {"processado": True}
        documento = app.state.banco["pagamentos"].find_one({"_id": dto.id})
        assert documento is not None
        assert documento["status"] == "SOLICITADO"
        assert eventos_do_outbox(app.state.banco, "PagamentoConfirmado") == []

    @pytest.mark.parametrize(
        "cabecalhos_do_mp",
        [{}, {"x-signature": "ts=1,v1=00", "x-request-id": "req-mp-1"}],
        ids=["sem-assinatura", "assinatura-errada"],
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

    def test_assinatura_invalida_e_contada(self, api: TestClient) -> None:
        def recusadas() -> float:
            valor = REGISTRY.get_sample_value(
                "pytstop_webhook_assinatura_invalida_total"
            )
            return valor or 0.0

        antes = recusadas()
        resposta = api.post(
            WEBHOOK,
            params={"data.id": "1"},
            json=notificacao("1"),
            headers=assinatura("2"),
        )
        assert resposta.status_code == 401
        assert recusadas() == antes + 1

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
    def test_checkout_html_com_botoes_que_levam_o_token(
        self, api: TestClient, app: FastAPI
    ) -> None:
        dto = solicitado(app)
        token = token_do_checkout(dto.checkout_url)
        resposta = api.get(f"/simulador/checkout/{dto.id}", params={"token": token})
        assert resposta.status_code == 200
        assert resposta.headers["content-type"].startswith("text/html")
        base = f"{URL_PUBLICA}/api/v1/simulador/pagamentos/{dto.id}"
        assert f'action="{base}/aprovar?token={token}"' in resposta.text
        assert f'action="{base}/recusar?token={token}"' in resposta.text
        assert "BRL 335.00" in resposta.text

    def test_aprovar_e_recusar_com_o_token(self, api: TestClient, app: FastAPI) -> None:
        aprovado = solicitado(app)
        token = {"token": token_do_checkout(aprovado.checkout_url)}
        caminho = f"/api/v1/simulador/pagamentos/{aprovado.id}"
        resposta = api.post(f"{caminho}/aprovar", params=token)
        assert resposta.status_code == 200
        assert resposta.json()["status"] == "CONFIRMADO"
        assert resposta.json()["notificacoes"][0]["status_provedor"] == "approved"
        assert api.post(f"{caminho}/recusar", params=token).status_code == 409

        recusado = solicitado(app)
        caminho = f"/api/v1/simulador/pagamentos/{recusado.id}/recusar"
        token = {"token": token_do_checkout(recusado.checkout_url)}
        # Cada recusa conta como a do provedor, ate PAGAMENTO_MAX_RECUSAS (3).
        corpos = [api.post(caminho, params=token).json() for _ in range(3)]
        assert [(c["status"], c["recusas"]) for c in corpos] == [
            ("SOLICITADO", 1),
            ("SOLICITADO", 2),
            ("RECUSADO", 3),
        ]

    @pytest.mark.parametrize(
        "token",
        [
            pytest.param(None, id="sem-token"),
            pytest.param("x.y.z", id="token-forjado"),
            pytest.param("outro-pagamento", id="token-de-outro-pagamento"),
        ],
    )
    def test_sem_o_token_do_pagamento_e_o_mesmo_404(
        self, api: TestClient, app: FastAPI, token: str | None
    ) -> None:
        alvo = solicitado(app)
        if token == "outro-pagamento":
            token = token_do_checkout(solicitado(app).checkout_url)
        params = {"token": token} if token else {}
        respostas = [
            api.get(f"/simulador/checkout/{alvo.id}", params=params),
            api.post(f"/api/v1/simulador/pagamentos/{alvo.id}/aprovar", params=params),
            api.post(f"/api/v1/simulador/pagamentos/{alvo.id}/recusar", params=params),
        ]
        assert [r.status_code for r in respostas] == [404, 404, 404]
        assert {r.json()["erro"]["codigo"] for r in respostas} == {
            "CHECKOUT_NAO_ENCONTRADO"
        }
        assert eventos_do_outbox(app.state.banco, "PagamentoConfirmado") == []

    def test_depois_da_compensacao_aprovar_no_simulador_da_409(
        self, api: TestClient, app: FastAPI
    ) -> None:
        # O simulador nao tem checkout a fechar (cancelar_cobranca nao faz nada):
        # quem impede pagar depois do cancelamento e o status CANCELADO.
        dto = solicitado(app)
        uow = MongoUnitOfWork(app.state.banco)
        EstornarPagamento(
            uow,
            MongoPagamentoRepository(uow),
            app.state.gateway_pagamento,
            app.state.metricas_pagamento,
        ).executar(ordem_id=dto.ordem_id, motivo="cancelamento")

        token = {"token": token_do_checkout(dto.checkout_url)}

        resposta = api.post(
            f"/api/v1/simulador/pagamentos/{dto.id}/aprovar", params=token
        )

        assert resposta.status_code == 409
        assert resposta.json()["erro"]["codigo"] == "TRANSICAO_STATUS_INVALIDA"
        assert eventos_do_outbox(app.state.banco, "PagamentoConfirmado") == []
        pagina = api.get(f"/simulador/checkout/{dto.id}", params=token)
        assert "<dd>CANCELADO</dd>" in pagina.text

    def test_token_expirado_e_o_mesmo_404(
        self, banco: Banco, jwks_publicado: dict[str, Any]
    ) -> None:
        relogio = RelogioFixo()
        app = criar_app(configuracao(), banco=banco, relogio=relogio)
        with TestClient(app) as cliente:
            dto = solicitado(app, relogio)
            relogio.avancar(minutes=61)
            resposta = cliente.post(
                f"/api/v1/simulador/pagamentos/{dto.id}/aprovar",
                params={"token": token_do_checkout(dto.checkout_url)},
            )
        assert resposta.status_code == 404
        assert resposta.json()["erro"]["codigo"] == "CHECKOUT_NAO_ENCONTRADO"

    def test_pagamento_inexistente_e_o_mesmo_404(self, api: TestClient) -> None:
        assert api.get(f"/simulador/checkout/{uuid4()}").status_code == 404
        resposta = api.post(f"/api/v1/simulador/pagamentos/{uuid4()}/aprovar")
        assert resposta.json()["erro"]["codigo"] == "CHECKOUT_NAO_ENCONTRADO"


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

    def test_simulador_nao_existe_nem_para_pagamento_real(
        self, api_mp: TestClient, banco: Banco
    ) -> None:
        pendente = pagamento()
        uow = MongoUnitOfWork(banco)
        uow.executar(lambda: MongoPagamentoRepository(uow).salvar(pendente))
        assert api_mp.get(f"/simulador/checkout/{pendente.id}").status_code == 404
        resposta = api_mp.post(f"/api/v1/simulador/pagamentos/{pendente.id}/aprovar")
        assert resposta.status_code == 404
        assert resposta.json()["erro"]["codigo"] == "ENTIDADE_NAO_ENCONTRADA"
        documento = banco["pagamentos"].find_one({"_id": pendente.id})
        assert documento is not None
        assert documento["status"] == "SOLICITADO"
        assert api_mp.get("/api/v1/saude").json()["modo"] == "mercadopago"

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

    def test_recusa_do_provedor_na_consulta_nao_da_200_para_ele_reenviar(
        self, api_mp: TestClient, banco: Banco
    ) -> None:
        pendente = pagamento()
        uow = MongoUnitOfWork(banco)
        uow.executar(lambda: MongoPagamentoRepository(uow).salvar(pendente))
        with respx.mock(base_url="https://api.mercadopago.com") as mp:
            mp.get("/v1/payments/1").respond(
                401, json={"message": "invalid access token", "status": 401}
            )
            resposta = api_mp.post(
                WEBHOOK,
                params={"data.id": "1", "type": "payment"},
                json=notificacao("1"),
                headers=assinatura("1"),
            )
        # 200 so para notificacao que nao e nossa (404 no provedor): credencial
        # revogada nao pode parar o reenvio do Mercado Pago.
        assert resposta.status_code == 409
        assert resposta.json()["erro"]["codigo"] == "GATEWAY_PAGAMENTO_RECUSOU"
        documento = banco["pagamentos"].find_one({"_id": pendente.id})
        assert documento is not None
        assert documento["status"] == "SOLICITADO"

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
