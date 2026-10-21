"""Teste de contrato do adapter do Mercado Pago (HTTP mockado com respx).

Payloads no formato da documentacao oficial do Mercado Pago:

- Criar preferencia (Checkout Pro): https://www.mercadopago.com.br/developers/pt/reference/online-payments/checkout-pro-preferences/create-preference/post
- Atualizar preferencia: https://www.mercadopago.com.br/developers/pt/reference/online-payments/checkout-pro-preferences/update-preference/put
- Obter pagamento: https://www.mercadopago.com.br/developers/pt/reference/online-payments/checkout-api-payments/get-payment/get
- Buscar pagamentos: https://www.mercadopago.com.br/developers/pt/reference/online-payments/checkout-pro-preferences/search-payments/get
- Criar reembolso: https://www.mercadopago.com.br/developers/pt/reference/online-payments/checkout-api-payments/create-refund/post
- Notificacoes (x-signature): https://www.mercadopago.com.br/developers/pt/docs/checkout-pro-preferences/payment-notifications
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
import respx
import structlog
from prometheus_client import REGISTRY

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.compartilhado.infraestrutura.circuit_breaker import CircuitBreaker
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.pagamento.aplicacao.ports import (
    EstornoEmProcessamentoError,
    GatewayPagamentoIndisponivelError,
    GatewayPagamentoRecusouError,
    ItemCobranca,
)
from src.pagamento.dominio.cobranca import SituacaoNoProvedor
from src.pagamento.dominio.estados import StatusNoProvedor
from src.pagamento.infraestrutura import mercadopago
from src.pagamento.infraestrutura.mercadopago import (
    FALHAS_PARA_ABRIR,
    SEGUNDOS_ABERTO,
    ConfiguracaoMercadoPago,
    FalhaTransitoriaError,
    MercadoPagoGateway,
    numero_json,
)
from src.pagamento.interfaces.assinatura_webhook import assinatura_webhook_valida
from tests.factories import AGORA, dinheiro

API = "https://api.mercadopago.com"
TOKEN = "TEST-0000000000000000-000000-00000000000000000000000000000000-000000000"
NOTIFICACAO = "https://billing.teste/api/v1/webhooks/mercadopago"

# Resposta de POST /checkout/preferences (exemplo da documentacao).
PREFERENCIA_CRIADA = {
    "id": "202809963-920c288b-4ebb-40be-966f-700250fa5370",
    "init_point": (
        "https://www.mercadopago.com/mla/checkout/start"
        "?pref_id=202809963-920c288b-4ebb-40be-966f-700250fa5370"
    ),
    "sandbox_init_point": (
        "https://sandbox.mercadopago.com/mla/checkout/pay"
        "?pref_id=202809963-920c288b-4ebb-40be-966f-700250fa5370"
    ),
    "date_created": "2022-11-17T10:37:52.000-05:00",
    "external_reference": "1643827245",
    "expires": True,
    "expiration_date_to": "2022-11-17T10:37:52.000-05:00",
    "collector_id": 202809963,
    "items": [
        {"title": "Dummy Item", "currency_id": "BRL", "quantity": 1, "unit_price": 24.5}
    ],
}


def pagamento_no_provedor(**campos: object) -> dict[str, object]:
    """Resposta de GET /v1/payments/{id} (exemplo da documentacao)."""
    return {
        "id": 1234567890,
        "date_created": "2017-08-31T11:26:38.000Z",
        "date_approved": "2017-08-31T11:26:38.000Z",
        "date_last_updated": "2017-08-31T11:26:38.000Z",
        "money_release_date": "2017-09-14T11:26:38.000Z",
        "payment_method_id": "visa",
        "payment_type_id": "credit_card",
        "status": "approved",
        "status_detail": "accredited",
        "currency_id": "BRL",
        "description": "Orcamento PytStop",
        "collector_id": 2,
        "external_reference": str(uuid4()),
        "transaction_amount": 335.0,
        "transaction_amount_refunded": 0,
        "coupon_amount": 0,
        "installments": 1,
        **campos,
    }


# Resposta 201 de POST /v1/payments/{id}/refunds (exemplo da documentacao).
REEMBOLSO_CRIADO = {
    "id": 1009042015,
    "payment_id": 1234567890,
    "amount": 335.0,
    "metadata": {},
    "source": [
        {"id": "1003743392", "name": "Firstname Lastname.", "type": "collector"}
    ],
    "date_created": "2021-11-24T13:58:49.312-04:00",
    "unique_sequence_number": None,
    "refund_mode": "standard",
    "adjustment_amount": 0,
    "status": "approved",
    "reason": None,
}


def erro_mp(status: int, mensagem: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "message": mensagem,
            "error": "bad_request",
            "status": status,
            "cause": [{"code": 2000, "description": mensagem, "data": None}],
        },
    )


class Esperas(list[float]):
    def __call__(self, segundos: float) -> None:
        self.append(segundos)


@pytest.fixture
def esperas() -> Esperas:
    return Esperas()


@pytest.fixture
def gateway(esperas: Esperas) -> Iterator[MercadoPagoGateway]:
    gateway = MercadoPagoGateway(
        ConfiguracaoMercadoPago(access_token=TOKEN, notification_url=NOTIFICACAO),
        breaker=CircuitBreaker("mercadopago-teste", falha=FalhaTransitoriaError),
        dormir=esperas,
    )
    yield gateway
    gateway.fechar()


def contador(operacao: str, resultado: str) -> float:
    valor = REGISTRY.get_sample_value(
        "pytstop_mercadopago_requisicoes_total",
        {"operacao": operacao, "resultado": resultado},
    )
    return valor or 0.0


class TestCriarCobranca:
    def test_envia_preferencia_no_formato_do_checkout_pro(
        self, gateway: MercadoPagoGateway
    ) -> None:
        pagamento_id = uuid4()
        expira_em = AGORA + timedelta(minutes=60)
        with respx.mock(base_url=API) as mp:
            rota = mp.post("/checkout/preferences").respond(
                201, json=PREFERENCIA_CRIADA
            )

            cobranca = gateway.criar_cobranca(
                pagamento_id=pagamento_id,
                itens=[
                    ItemCobranca(
                        "SRV-TROCA-OLEO", "Troca de oleo", 1, dinheiro("120.00")
                    ),
                    ItemCobranca("PEC-OLEO-5W30", "Oleo 5W30", 4, dinheiro("45.00")),
                ],
                expira_em=expira_em,
            )

        assert cobranca.referencia == PREFERENCIA_CRIADA["id"]
        assert cobranca.checkout_url == PREFERENCIA_CRIADA["init_point"]
        requisicao = rota.calls.last.request
        assert requisicao.headers["Authorization"] == f"Bearer {TOKEN}"
        corpo = json.loads(requisicao.content, parse_float=Decimal)
        assert corpo == {
            "items": [
                {
                    "id": "SRV-TROCA-OLEO",
                    "title": "Troca de oleo",
                    "quantity": 1,
                    "currency_id": "BRL",
                    "unit_price": Decimal("120.0"),
                },
                {
                    "id": "PEC-OLEO-5W30",
                    "title": "Oleo 5W30",
                    "quantity": 4,
                    "currency_id": "BRL",
                    "unit_price": Decimal("45.0"),
                },
            ],
            "external_reference": str(pagamento_id),
            "notification_url": NOTIFICACAO,
            "expires": True,
            "expiration_date_to": "2026-10-06T13:00:00.000+00:00",
            "date_of_expiration": "2026-10-06T13:00:00.000+00:00",
        }

    def test_envia_a_moeda_de_cada_item_e_nao_so_brl(
        self, gateway: MercadoPagoGateway
    ) -> None:
        with respx.mock(base_url=API) as mp:
            rota = mp.post("/checkout/preferences").respond(
                201, json=PREFERENCIA_CRIADA
            )

            gateway.criar_cobranca(
                pagamento_id=uuid4(),
                itens=[
                    ItemCobranca(
                        "SRV-USD",
                        "Servico em dolar",
                        1,
                        Dinheiro(Decimal("10.00"), "USD"),
                    ),
                    ItemCobranca("PEC-BRL", "Peca em real", 1, dinheiro("5.00")),
                ],
                expira_em=AGORA,
            )

        corpo = json.loads(rota.calls.last.request.content)
        assert [item["currency_id"] for item in corpo["items"]] == ["USD", "BRL"]

    def test_nao_repete_porque_criar_preferencia_nao_e_idempotente(
        self, gateway: MercadoPagoGateway, esperas: Esperas
    ) -> None:
        itens = [ItemCobranca("SRV-X", "X", 1, dinheiro("1.00"))]
        with respx.mock(base_url=API) as mp:
            rota = mp.post("/checkout/preferences").respond(503)
            with pytest.raises(GatewayPagamentoIndisponivelError):
                gateway.criar_cobranca(
                    pagamento_id=uuid4(), itens=itens, expira_em=AGORA
                )
        assert rota.call_count == 1
        assert esperas == []

    def test_erro_400_vira_recusa_com_mensagem_fixa(
        self, gateway: MercadoPagoGateway
    ) -> None:
        itens = [ItemCobranca("SRV-X", "X", 1, dinheiro("1.00"))]
        with respx.mock(base_url=API) as mp:
            mp.post("/checkout/preferences").mock(
                return_value=erro_mp(400, "unit_price invalid")
            )
            with pytest.raises(
                GatewayPagamentoRecusouError,
                match="Provedor de pagamento recusou a operacao",
            ):
                gateway.criar_cobranca(
                    pagamento_id=uuid4(), itens=itens, expira_em=AGORA
                )


class TestConsultarPagamento:
    def test_pagamento_aprovado(self, gateway: MercadoPagoGateway) -> None:
        resposta = pagamento_no_provedor()
        antes = contador("consultar_pagamento", "sucesso")
        with respx.mock(base_url=API) as mp:
            mp.get("/v1/payments/1234567890").respond(200, json=resposta)
            situacao = gateway.consultar_pagamento("1234567890")

        assert situacao is not None
        assert situacao.referencia == "1234567890"
        assert situacao.referencia_externa == resposta["external_reference"]
        assert situacao.status is StatusNoProvedor.APROVADO
        assert situacao.status_provedor == "approved"
        assert situacao.detalhe == "accredited"
        assert situacao.valor == dinheiro("335.00")
        assert contador("consultar_pagamento", "sucesso") == antes + 1

    @pytest.mark.parametrize(
        "valor", [335.0, "335.00", 335], ids=["numero", "texto", "inteiro"]
    )
    def test_valor_numero_ou_string_vira_decimal_exato(
        self, gateway: MercadoPagoGateway, valor: object
    ) -> None:
        with respx.mock(base_url=API) as mp:
            mp.get("/v1/payments/1").respond(
                200, json=pagamento_no_provedor(transaction_amount=valor)
            )
            situacao = gateway.consultar_pagamento("1")
        assert situacao is not None
        assert situacao.valor == dinheiro("335.00")

    @pytest.mark.parametrize(
        ("status", "esperado"),
        [
            # Recusa e contada pelo agregado ate PAGAMENTO_MAX_RECUSAS; o resto
            # nao muda a cobranca (contestacao nao e estorno).
            ("approved", StatusNoProvedor.APROVADO),
            ("rejected", StatusNoProvedor.RECUSADO),
            ("refunded", StatusNoProvedor.ESTORNADO),
            ("charged_back", StatusNoProvedor.EM_ANDAMENTO),
            ("cancelled", StatusNoProvedor.EM_ANDAMENTO),
            ("pending", StatusNoProvedor.EM_ANDAMENTO),
            ("in_process", StatusNoProvedor.EM_ANDAMENTO),
            ("authorized", StatusNoProvedor.EM_ANDAMENTO),
            ("in_mediation", StatusNoProvedor.EM_ANDAMENTO),
        ],
        ids=lambda valor: str(valor),
    )
    def test_mapa_de_status(
        self, gateway: MercadoPagoGateway, status: str, esperado: StatusNoProvedor
    ) -> None:
        with respx.mock(base_url=API) as mp:
            mp.get("/v1/payments/1").respond(
                200, json=pagamento_no_provedor(status=status)
            )
            situacao = gateway.consultar_pagamento("1")
        assert situacao is not None
        assert situacao.status is esperado
        assert situacao.status_provedor == status

    def test_status_desconhecido_nao_muda_a_cobranca_e_e_contado(
        self, gateway: MercadoPagoGateway
    ) -> None:
        antes = contador("consultar_pagamento", "status_desconhecido")
        with respx.mock(base_url=API) as mp:
            mp.get("/v1/payments/1").respond(
                200, json=pagamento_no_provedor(status="novo_status")
            )
            situacao = gateway.consultar_pagamento("1")
        assert situacao is not None
        assert situacao.status is StatusNoProvedor.EM_ANDAMENTO
        assert contador("consultar_pagamento", "status_desconhecido") == antes + 1

    def test_moeda_vem_do_provedor(self, gateway: MercadoPagoGateway) -> None:
        with respx.mock(base_url=API) as mp:
            mp.get("/v1/payments/1").respond(
                200, json=pagamento_no_provedor(currency_id="USD")
            )
            situacao = gateway.consultar_pagamento("1")
        assert situacao is not None
        assert situacao.valor is not None
        assert situacao.valor.moeda == "USD"

    def test_sem_valor_e_sem_referencia_externa(
        self, gateway: MercadoPagoGateway
    ) -> None:
        resposta = pagamento_no_provedor(external_reference=None)
        del resposta["transaction_amount"]
        with respx.mock(base_url=API) as mp:
            mp.get("/v1/payments/1").respond(200, json=resposta)
            situacao = gateway.consultar_pagamento("1")
        assert situacao is not None
        assert situacao.referencia_externa is None
        assert situacao.valor is None

    def test_404_e_pagamento_desconhecido(self, gateway: MercadoPagoGateway) -> None:
        with respx.mock(base_url=API) as mp:
            mp.get("/v1/payments/9").mock(
                return_value=erro_mp(404, "Payment not found")
            )
            assert gateway.consultar_pagamento("9") is None

    @pytest.mark.parametrize("status", [400, 401, 403])
    def test_4xx_que_nao_e_404_e_recusa_e_nao_pagamento_desconhecido(
        self, gateway: MercadoPagoGateway, status: int
    ) -> None:
        # Token revogado ou conta errada nao e "referencia que nao e nossa":
        # devolver None faria o webhook responder 200, e o Mercado Pago pararia
        # de reenviar a notificacao.
        antes = contador("consultar_pagamento", "recusado")
        with respx.mock(base_url=API) as mp:
            rota = mp.get("/v1/payments/1").mock(
                return_value=erro_mp(status, "invalid access token")
            )
            with pytest.raises(
                GatewayPagamentoRecusouError,
                match="Provedor de pagamento recusou a operacao",
            ):
                gateway.consultar_pagamento("1")
        assert rota.call_count == 1  # recusa nao e repetida
        assert contador("consultar_pagamento", "recusado") == antes + 1

    def test_repete_falha_transitoria_com_backoff(
        self, gateway: MercadoPagoGateway, esperas: Esperas
    ) -> None:
        with respx.mock(base_url=API) as mp:
            rota = mp.get("/v1/payments/1").mock(
                side_effect=[
                    httpx.Response(503),
                    httpx.ConnectTimeout("timeout"),
                    httpx.Response(200, json=pagamento_no_provedor()),
                ]
            )
            situacao = gateway.consultar_pagamento("1")
        assert situacao is not None
        assert rota.call_count == 3
        assert len(esperas) == 2
        assert 0.2 <= esperas[0] < 0.3
        assert 0.4 <= esperas[1] < 0.5

    def test_esgota_as_tentativas_e_fica_indisponivel(
        self, gateway: MercadoPagoGateway
    ) -> None:
        with respx.mock(base_url=API) as mp:
            rota = mp.get("/v1/payments/1").respond(429)
            with pytest.raises(GatewayPagamentoIndisponivelError):
                gateway.consultar_pagamento("1")
        assert rota.call_count == 3

    @pytest.mark.parametrize("referencia", ["../users/me", "1?x=1", "", "a" * 65])
    def test_referencia_que_mudaria_a_url_nem_sai_daqui(
        self, gateway: MercadoPagoGateway, referencia: str
    ) -> None:
        with respx.mock(base_url=API, assert_all_called=False) as mp:
            rota = mp.route()
            assert gateway.consultar_pagamento(referencia) is None
        assert rota.call_count == 0


class TestBuscarPorReferenciaExterna:
    def test_busca_as_tentativas_da_cobranca(self, gateway: MercadoPagoGateway) -> None:
        pagamento_id = str(uuid4())
        resultados = [
            pagamento_no_provedor(
                id=1, status="rejected", external_reference=pagamento_id
            ),
            pagamento_no_provedor(
                id=2, status="approved", external_reference=pagamento_id
            ),
        ]
        with respx.mock(base_url=API) as mp:
            rota = mp.get("/v1/payments/search").respond(
                200,
                json={
                    "paging": {"total": 2, "limit": 30, "offset": 0},
                    "results": resultados,
                },
            )
            situacoes = gateway.buscar_por_referencia_externa(pagamento_id)
        parametros = rota.calls.last.request.url.params
        assert parametros["external_reference"] == pagamento_id
        assert (parametros["sort"], parametros["criteria"]) == ("date_created", "asc")
        assert [(s.referencia, s.status) for s in situacoes] == [
            ("1", StatusNoProvedor.RECUSADO),
            ("2", StatusNoProvedor.APROVADO),
        ]
        assert {s.referencia_externa for s in situacoes} == {pagamento_id}

    def test_sem_tentativas(self, gateway: MercadoPagoGateway) -> None:
        with respx.mock(base_url=API) as mp:
            mp.get("/v1/payments/search").respond(
                200, json={"paging": {"total": 0}, "results": []}
            )
            assert gateway.buscar_por_referencia_externa(str(uuid4())) == []

    def test_repete_falha_transitoria(
        self, gateway: MercadoPagoGateway, esperas: Esperas
    ) -> None:
        with respx.mock(base_url=API) as mp:
            rota = mp.get("/v1/payments/search").mock(
                side_effect=[
                    httpx.Response(503),
                    httpx.Response(200, json={"results": []}),
                ]
            )
            assert gateway.buscar_por_referencia_externa("x") == []
        assert (rota.call_count, len(esperas)) == (2, 1)

    def test_status_desconhecido_na_busca_conta_na_operacao_da_busca(
        self, gateway: MercadoPagoGateway
    ) -> None:
        antes = contador("buscar_pagamentos", "status_desconhecido")
        with respx.mock(base_url=API) as mp:
            mp.get("/v1/payments/search").respond(
                200, json={"results": [pagamento_no_provedor(status="novo_status")]}
            )
            [situacao] = gateway.buscar_por_referencia_externa("x")
        assert situacao.status is StatusNoProvedor.EM_ANDAMENTO
        assert contador("buscar_pagamentos", "status_desconhecido") == antes + 1


class TestEstornar:
    def test_estorno_total_com_chave_de_idempotencia(
        self, gateway: MercadoPagoGateway
    ) -> None:
        with respx.mock(base_url=API) as mp:
            rota = mp.post("/v1/payments/1234567890/refunds").respond(
                201, json=REEMBOLSO_CRIADO
            )
            gateway.estornar("1234567890", chave_idempotencia="msg-estorno-1")
        requisicao = rota.calls.last.request
        assert requisicao.headers["X-Idempotency-Key"] == "msg-estorno-1"
        assert json.loads(requisicao.content) == {}

    def test_repete_com_a_mesma_chave_em_falha_transitoria(
        self, gateway: MercadoPagoGateway
    ) -> None:
        with respx.mock(base_url=API) as mp:
            rota = mp.post("/v1/payments/1/refunds").mock(
                side_effect=[
                    httpx.Response(500),
                    httpx.Response(201, json=REEMBOLSO_CRIADO),
                ]
            )
            gateway.estornar("1", chave_idempotencia="msg-2")
        chaves = {c.request.headers["X-Idempotency-Key"] for c in rota.calls}
        assert (rota.call_count, chaves) == (2, {"msg-2"})

    def test_recusa_do_provedor_nao_e_repetida(
        self, gateway: MercadoPagoGateway
    ) -> None:
        with respx.mock(base_url=API) as mp:
            rota = mp.post("/v1/payments/1/refunds").mock(
                return_value=erro_mp(400, "Payment already refunded")
            )
            with pytest.raises(
                GatewayPagamentoRecusouError,
                match="Provedor de pagamento recusou a operacao",
            ):
                gateway.estornar("1", chave_idempotencia="msg-3")
        assert rota.call_count == 1

    @pytest.mark.parametrize("status", ["in_process", "pending"])
    def test_estorno_ainda_em_processamento_e_transitorio(
        self, gateway: MercadoPagoGateway, status: str
    ) -> None:
        with respx.mock(base_url=API) as mp:
            mp.post("/v1/payments/1/refunds").respond(
                201, json={**REEMBOLSO_CRIADO, "status": status}
            )
            with pytest.raises(EstornoEmProcessamentoError):
                gateway.estornar("1", chave_idempotencia="msg-5")

    @pytest.mark.parametrize("status", ["rejected", "cancelled"])
    def test_estorno_criado_e_recusado(
        self, gateway: MercadoPagoGateway, status: str
    ) -> None:
        with respx.mock(base_url=API) as mp:
            mp.post("/v1/payments/1/refunds").respond(
                201, json={**REEMBOLSO_CRIADO, "status": status}
            )
            with pytest.raises(
                GatewayPagamentoRecusouError,
                match="Provedor de pagamento recusou a operacao",
            ):
                gateway.estornar("1", chave_idempotencia="msg-6")

    def test_estorno_sem_status_nao_e_dado_como_concluido(
        self, gateway: MercadoPagoGateway
    ) -> None:
        with respx.mock(base_url=API) as mp:
            mp.post("/v1/payments/1/refunds").respond(201, json={"id": 1})
            with pytest.raises(EstornoEmProcessamentoError):
                gateway.estornar("1", chave_idempotencia="msg-7")

    @pytest.mark.parametrize(
        "corpo", ["", "[]", "<html>"], ids=["vazio", "lista", "html"]
    )
    def test_corpo_fora_do_contrato_repete_com_a_mesma_chave(
        self, gateway: MercadoPagoGateway, corpo: str
    ) -> None:
        with respx.mock(base_url=API) as mp:
            rota = mp.post("/v1/payments/1/refunds").respond(201, text=corpo)
            with pytest.raises(GatewayPagamentoIndisponivelError) as erro:
                gateway.estornar("1", chave_idempotencia="msg-8")
        assert type(erro.value) is GatewayPagamentoIndisponivelError
        chaves = {c.request.headers["X-Idempotency-Key"] for c in rota.calls}
        assert (rota.call_count, chaves) == (3, {"msg-8"})

    def test_erro_sem_corpo_json(self, gateway: MercadoPagoGateway) -> None:
        with respx.mock(base_url=API) as mp:
            mp.post("/v1/payments/1/refunds").respond(404, text="not found")
            with pytest.raises(
                GatewayPagamentoRecusouError,
                match="Provedor de pagamento recusou a operacao",
            ):
                gateway.estornar("1", chave_idempotencia="msg-4")

    def test_referencia_invalida_e_recusada_sem_chamar(
        self, gateway: MercadoPagoGateway
    ) -> None:
        with pytest.raises(
            GatewayPagamentoRecusouError,
            match="Provedor de pagamento recusou a operacao",
        ):
            gateway.estornar("../1", chave_idempotencia="msg")


class TestCancelarCobranca:
    PREFERENCIA = "202809963-920c288b-4ebb-40be-966f-700250fa5370"

    def test_expira_a_preferencia_agora(self, esperas: Esperas) -> None:
        gateway = MercadoPagoGateway(
            ConfiguracaoMercadoPago(access_token=TOKEN, notification_url=NOTIFICACAO),
            breaker=CircuitBreaker("mp-cancelar", falha=FalhaTransitoriaError),
            dormir=esperas,
            relogio=lambda: AGORA,
        )
        try:
            with respx.mock(base_url=API) as mp:
                rota = mp.put(f"/checkout/preferences/{self.PREFERENCIA}").mock(
                    side_effect=[
                        httpx.Response(500),
                        httpx.Response(200, json=PREFERENCIA_CRIADA),
                    ]
                )
                gateway.cancelar_cobranca(self.PREFERENCIA)
        finally:
            gateway.fechar()
        # Idempotente: a falha transitoria e repetida.
        assert rota.call_count == 2
        assert json.loads(rota.calls.last.request.content) == {
            "expires": True,
            "expiration_date_to": "2026-10-06T12:00:00.000+00:00",
            "date_of_expiration": "2026-10-06T12:00:00.000+00:00",
        }

    def test_referencia_invalida_e_recusada_sem_chamar(
        self, gateway: MercadoPagoGateway
    ) -> None:
        with pytest.raises(
            GatewayPagamentoRecusouError,
            match="Provedor de pagamento recusou a operacao",
        ):
            gateway.cancelar_cobranca("../preferences")

    def test_redirecionamento_nao_e_sucesso_mesmo_com_o_corpo_ignorado(
        self, gateway: MercadoPagoGateway
    ) -> None:
        # O corpo do cancelamento nao e lido: o status 3xx e o unico sinal de que
        # o checkout nao foi fechado, e conta como resposta fora do contrato.
        invalidas = contador("cancelar_cobranca", "resposta_invalida")
        sucessos = contador("cancelar_cobranca", "sucesso")
        with respx.mock(base_url=API) as mp:
            rota = mp.put(f"/checkout/preferences/{self.PREFERENCIA}").respond(
                302, headers={"Location": "https://x.teste"}
            )
            with pytest.raises(GatewayPagamentoIndisponivelError):
                gateway.cancelar_cobranca(self.PREFERENCIA)
        assert rota.call_count == 3
        assert contador("cancelar_cobranca", "resposta_invalida") == invalidas + 3
        assert contador("cancelar_cobranca", "sucesso") == sucessos


class TestRespostaForaDoContrato:
    """3xx ou 2xx com corpo fora do contrato: falha transitoria lida dentro da
    chamada protegida (repete, conta no disjuntor) e nunca conta como sucesso."""

    @pytest.mark.parametrize(
        ("status", "opcoes"),
        [
            pytest.param(200, {"text": "<html>manutencao</html>"}, id="200-html"),
            pytest.param(302, {"headers": {"Location": "https://x.teste"}}, id="302"),
            pytest.param(200, {"json": {"id": 1}}, id="sem-status"),
            pytest.param(200, {"json": {"status": "approved"}}, id="sem-id"),
            pytest.param(200, {"json": []}, id="lista"),
            pytest.param(
                200,
                {"json": pagamento_no_provedor(transaction_amount="abc")},
                id="valor-invalido",
            ),
            pytest.param(
                200,
                {"json": pagamento_no_provedor(transaction_amount="1E+30")},
                id="valor-fora-do-teto",
            ),
            pytest.param(
                200, {"json": pagamento_no_provedor(currency_id="real")}, id="moeda"
            ),
        ],
    )
    def test_consulta_repete_e_fica_indisponivel(
        self, gateway: MercadoPagoGateway, status: int, opcoes: dict[str, Any]
    ) -> None:
        invalidas = contador("consultar_pagamento", "resposta_invalida")
        sucessos = contador("consultar_pagamento", "sucesso")
        with respx.mock(base_url=API) as mp:
            rota = mp.get("/v1/payments/1").respond(status, **opcoes)
            with pytest.raises(GatewayPagamentoIndisponivelError):
                gateway.consultar_pagamento("1")
        assert rota.call_count == 3
        assert contador("consultar_pagamento", "resposta_invalida") == invalidas + 3
        assert contador("consultar_pagamento", "sucesso") == sucessos

    @pytest.mark.parametrize(
        "corpo",
        [
            {"id": "pref-1"},
            {"init_point": "https://x.teste"},
            {"id": "", "init_point": 1},
        ],
        ids=["sem-init-point", "sem-id", "tipos-errados"],
    )
    def test_preferencia_criada_sem_os_campos_nao_repete(
        self, gateway: MercadoPagoGateway, corpo: dict[str, object]
    ) -> None:
        antes = contador("criar_cobranca", "resposta_invalida")
        itens = [ItemCobranca("SRV-X", "X", 1, dinheiro("1.00"))]
        with respx.mock(base_url=API) as mp:
            rota = mp.post("/checkout/preferences").respond(201, json=corpo)
            with pytest.raises(GatewayPagamentoIndisponivelError):
                gateway.criar_cobranca(
                    pagamento_id=uuid4(), itens=itens, expira_em=AGORA
                )
        assert rota.call_count == 1
        assert contador("criar_cobranca", "resposta_invalida") == antes + 1

    @pytest.mark.parametrize(
        "corpo",
        [{"paging": {"total": 0}}, {"results": {"id": 1}}, {"results": [{"id": 1}]}],
        ids=["sem-results", "results-objeto", "tentativa-sem-status"],
    )
    def test_busca_fora_do_contrato(
        self, gateway: MercadoPagoGateway, corpo: dict[str, object]
    ) -> None:
        with respx.mock(base_url=API) as mp:
            rota = mp.get("/v1/payments/search").respond(200, json=corpo)
            with pytest.raises(GatewayPagamentoIndisponivelError):
                gateway.buscar_por_referencia_externa("x")
        assert rota.call_count == 3

    def test_resposta_invalida_abre_o_disjuntor(self, esperas: Esperas) -> None:
        gateway = MercadoPagoGateway(
            ConfiguracaoMercadoPago(
                access_token=TOKEN, notification_url=NOTIFICACAO, tentativas=1
            ),
            breaker=CircuitBreaker(
                "mp-resposta-invalida", falha=FalhaTransitoriaError, limite_falhas=2
            ),
            dormir=esperas,
        )
        try:
            with respx.mock(base_url=API) as mp:
                rota = mp.get("/v1/payments/1").respond(200, text="<html>")
                for _ in range(3):
                    with pytest.raises(GatewayPagamentoIndisponivelError):
                        gateway.consultar_pagamento("1")
            assert rota.call_count == 2
        finally:
            gateway.fechar()

    def test_recusa_4xx_nao_conta_no_disjuntor(self, esperas: Esperas) -> None:
        breaker = CircuitBreaker(
            "mp-recusa", falha=FalhaTransitoriaError, limite_falhas=1
        )
        gateway = MercadoPagoGateway(
            ConfiguracaoMercadoPago(access_token=TOKEN, notification_url=NOTIFICACAO),
            breaker=breaker,
            dormir=esperas,
        )
        try:
            with respx.mock(base_url=API) as mp:
                mp.post("/v1/payments/1/refunds").mock(
                    return_value=erro_mp(400, "invalid")
                )
                with pytest.raises(GatewayPagamentoRecusouError):
                    gateway.estornar("1", chave_idempotencia="k")
            assert not breaker.aberto
        finally:
            gateway.fechar()

    def test_404_fora_da_consulta_e_recusa_contada_como_nao_encontrado(
        self, gateway: MercadoPagoGateway
    ) -> None:
        antes = contador("cancelar_cobranca", "nao_encontrado")
        with respx.mock(base_url=API) as mp:
            mp.put("/checkout/preferences/pref-1").mock(
                return_value=erro_mp(404, "preference not found")
            )
            with pytest.raises(
                GatewayPagamentoRecusouError,
                match="Provedor de pagamento recusou a operacao",
            ):
                gateway.cancelar_cobranca("pref-1")
        assert contador("cancelar_cobranca", "nao_encontrado") == antes + 1


EVENTO_DE_RECUSA = "mercadopago_request_rejected"
# O que o pipeline de log poe em toda linha; o resto e do evento.
CAMPOS_DO_PIPELINE = frozenset(
    {
        "event",
        "level",
        "logger",
        "timestamp",
        "git_sha",
        "git_date",
        "trace_id",
        "span_id",
    }
)
NIVEIS_DE_AVISO = frozenset({"warning", "error", "critical"})


class LogDoAdaptador:
    """O log como o operador o le: as linhas JSON do pipeline de producao."""

    def __init__(self, saida: io.StringIO) -> None:
        self._saida = saida

    @property
    def texto(self) -> str:
        return self._saida.getvalue()

    def linhas(self) -> list[dict[str, Any]]:
        return [json.loads(linha) for linha in self.texto.splitlines() if linha]

    def eventos(self, nome: str) -> list[dict[str, Any]]:
        return [linha for linha in self.linhas() if linha["event"] == nome]

    def recusas(self) -> list[dict[str, Any]]:
        return self.eventos(EVENTO_DE_RECUSA)

    def avisos(self) -> list[dict[str, Any]]:
        """De WARNING para cima: o httpx escreve cada chamada em INFO."""
        return [linha for linha in self.linhas() if linha["level"] in NIVEIS_DE_AVISO]


def campos_do_evento(linha: dict[str, Any]) -> dict[str, Any]:
    return {
        campo: valor
        for campo, valor in linha.items()
        if campo not in CAMPOS_DO_PIPELINE
    }


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> Iterator[LogDoAdaptador]:
    """``configurar_logging`` de producao (com o scrub de PII) numa saida de teste.

    O structlog guarda a configuracao de quando o logger foi usado pela primeira
    vez: o do modulo e trocado por um novo, que nasce com a de agora. No fim volta
    tudo como estava, inclusive os loggers que o ``configurar_logging`` mexe.
    """
    outros = ("pika", "uvicorn", "uvicorn.error", "uvicorn.access")
    antes = {
        nome: (
            logging.getLogger(nome).handlers[:],
            logging.getLogger(nome).propagate,
            logging.getLogger(nome).level,
        )
        for nome in outros
    }
    raiz = logging.getLogger()
    handlers, nivel_da_raiz = raiz.handlers[:], raiz.level
    configuracao = structlog.get_config()
    saida = io.StringIO()
    configurar_logging(saida)
    # O pipeline fica na frente e os handlers que o pytest usa para capturar
    # seguem no lugar: ele mesmo os tira ao fim de cada fase do teste.
    json_do_pipeline = raiz.handlers[0]
    raiz.handlers = [json_do_pipeline, *handlers]
    monkeypatch.setattr(mercadopago, "_log", structlog.get_logger(mercadopago.__name__))
    yield LogDoAdaptador(saida)
    structlog.configure(**configuracao)
    raiz.handlers = [h for h in raiz.handlers if h is not json_do_pipeline]
    raiz.setLevel(nivel_da_raiz)
    for nome, (handlers_do_logger, propagate, nivel) in antes.items():
        logger = logging.getLogger(nome)
        logger.handlers, logger.propagate = handlers_do_logger, propagate
        logger.setLevel(nivel)


def _criar_cobranca(gateway: MercadoPagoGateway) -> object:
    return gateway.criar_cobranca(
        pagamento_id=uuid4(),
        itens=[ItemCobranca("SRV-X", "X", 1, dinheiro("1.00"))],
        expira_em=AGORA,
    )


# operacao -> (metodo, caminho, chamada ao gateway): as cinco do adapter.
OPERACOES: dict[str, tuple[str, str, Callable[[MercadoPagoGateway], object]]] = {
    "criar_cobranca": ("POST", "/checkout/preferences", _criar_cobranca),
    "cancelar_cobranca": (
        "PUT",
        "/checkout/preferences/pref-1",
        lambda gateway: gateway.cancelar_cobranca("pref-1"),
    ),
    "consultar_pagamento": (
        "GET",
        "/v1/payments/1",
        lambda gateway: gateway.consultar_pagamento("1"),
    ),
    "buscar_pagamentos": (
        "GET",
        "/v1/payments/search",
        lambda gateway: gateway.buscar_por_referencia_externa("x"),
    ),
    "estornar": (
        "POST",
        "/v1/payments/1/refunds",
        lambda gateway: gateway.estornar("1", chave_idempotencia="k"),
    ),
}


def recusar(
    gateway: MercadoPagoGateway, operacao: str, resposta: httpx.Response
) -> GatewayPagamentoRecusouError:
    """Faz a ``operacao`` receber ``resposta`` do provedor e devolve a recusa."""
    metodo, caminho, chamar = OPERACOES[operacao]
    with respx.mock(base_url=API) as mp:
        mp.route(method=metodo, path=caminho).mock(return_value=resposta)
        with pytest.raises(GatewayPagamentoRecusouError) as recusa:
            chamar(gateway)
    return recusa.value


def uma_recusa(log: LogDoAdaptador) -> dict[str, Any]:
    [linha] = log.recusas()
    return campos_do_evento(linha)


class TestLogDaRecusa:
    """4xx do provedor: um WARNING com o status HTTP, o ``error`` e os
    ``cause[].code`` (vocabulario curto e fixo), nunca o texto livre."""

    def test_400_loga_operacao_status_erro_e_codigos_e_mais_nada(
        self, gateway: MercadoPagoGateway, log: LogDoAdaptador
    ) -> None:
        antes = contador("criar_cobranca", "recusado")
        corpo = {
            "message": "invalid items",
            "error": "bad_request",
            "status": 400,
            "cause": [
                {"code": "4001", "description": "unit_price invalid", "data": None},
                {"code": "x_y"},
            ],
        }

        recusa = recusar(gateway, "criar_cobranca", httpx.Response(400, json=corpo))

        [linha] = log.recusas()
        assert linha["level"] == "warning"
        assert campos_do_evento(linha) == {
            "operacao": "criar_cobranca",
            "status_http": 400,
            "erro": "bad_request",
            "causas": ["4001", "x_y"],
        }
        # Um WARNING so, a metrica conta como antes e a excecao leva a mesma frase.
        assert log.avisos() == [linha]
        assert contador("criar_cobranca", "recusado") == antes + 1
        assert str(recusa) == "Provedor de pagamento recusou a operacao"

    def test_texto_livre_do_provedor_nunca_aparece_no_log(
        self,
        gateway: MercadoPagoGateway,
        log: LogDoAdaptador,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        email = "cliente.sigiloso@example.com"
        segredo = (
            "APP_USR-0000000000000000-000000-00000000000000000000000000000000-0000000"
        )
        corpo = {
            "message": f"SENTINELA-MESSAGE {email} {segredo}",
            "error": "Texto Livre SENTINELA-ERROR",
            "status": 400,
            "cause": [
                {
                    "code": "texto livre SENTINELA-CODE",
                    "description": f"SENTINELA-DESCRIPTION {email} {segredo}",
                    "message": "SENTINELA-CAUSE-MESSAGE",
                    "data": "SENTINELA-DATA",
                },
                {"code": "4001", "description": f"SENTINELA-OUTRA {email}"},
            ],
            "detail": "SENTINELA-DETAIL",
        }

        recusa = recusar(gateway, "criar_cobranca", httpx.Response(400, json=corpo))

        saida = capsys.readouterr()
        visivel = log.texto + saida.out + saida.err
        for texto in ("SENTINELA", "cliente.sigiloso", segredo, TOKEN):
            assert texto not in visivel
        assert uma_recusa(log) == {
            "operacao": "criar_cobranca",
            "status_http": 400,
            "causas": ["4001"],
        }
        # A excecao tambem bloqueia o texto livre antes dos demais destinos.
        assert str(recusa) == "Provedor de pagamento recusou a operacao"

    def test_so_o_evento_vai_para_o_log_sem_url_cabecalho_nem_referencia(
        self, gateway: MercadoPagoGateway, log: LogDoAdaptador
    ) -> None:
        recusar(gateway, "cancelar_cobranca", erro_mp(400, "x"))

        [linha] = log.recusas()
        assert "pref-1" not in json.dumps(linha)
        assert set(campos_do_evento(linha)) == {
            "operacao",
            "status_http",
            "erro",
            "causas",
        }

    @pytest.mark.parametrize(
        "status",
        [
            pytest.param(400, id="400-requisicao-invalida"),
            pytest.param(401, id="401-sem-credencial"),
            pytest.param(403, id="403-sem-permissao"),
            pytest.param(409, id="409-conflito"),
            pytest.param(422, id="422-nao-processavel"),
        ],
    )
    def test_cada_status_4xx_sai_no_log(
        self, gateway: MercadoPagoGateway, log: LogDoAdaptador, status: int
    ) -> None:
        recusar(gateway, "criar_cobranca", erro_mp(status, "x"))

        assert uma_recusa(log) == {
            "operacao": "criar_cobranca",
            "status_http": status,
            "erro": "bad_request",
            "causas": ["2000"],
        }

    @pytest.mark.parametrize("operacao", list(OPERACOES))
    def test_cada_operacao_sai_com_o_proprio_nome_e_uma_linha_so(
        self, gateway: MercadoPagoGateway, log: LogDoAdaptador, operacao: str
    ) -> None:
        recusar(gateway, operacao, erro_mp(400, "x"))

        assert [linha["operacao"] for linha in log.recusas()] == [operacao]
        assert len(log.avisos()) == 1

    def test_404_da_consulta_de_pagamento_e_esperado_e_nao_gera_log(
        self, gateway: MercadoPagoGateway, log: LogDoAdaptador
    ) -> None:
        antes = contador("consultar_pagamento", "nao_encontrado")
        with respx.mock(base_url=API) as mp:
            mp.get("/v1/payments/9").mock(
                return_value=erro_mp(404, "Payment not found")
            )
            assert gateway.consultar_pagamento("9") is None

        assert log.recusas() == []
        assert log.avisos() == []
        assert contador("consultar_pagamento", "nao_encontrado") == antes + 1

    @pytest.mark.parametrize(
        "operacao",
        [operacao for operacao in OPERACOES if operacao != "consultar_pagamento"],
    )
    def test_404_das_outras_operacoes_e_recusa_de_verdade_e_sai_no_log(
        self, gateway: MercadoPagoGateway, log: LogDoAdaptador, operacao: str
    ) -> None:
        recusar(gateway, operacao, erro_mp(404, "not found"))

        linha = uma_recusa(log)
        assert (linha["operacao"], linha["status_http"]) == (operacao, 404)

    @pytest.mark.parametrize(
        "operacao",
        [
            pytest.param("criar_cobranca", id="criar-sem-repeticao"),
            pytest.param("consultar_pagamento", id="consultar-com-repeticao"),
        ],
    )
    @pytest.mark.parametrize(
        "status",
        [
            pytest.param(429, id="429-muitas-requisicoes"),
            pytest.param(500, id="500-erro-interno"),
            pytest.param(502, id="502-gateway-ruim"),
            pytest.param(503, id="503-indisponivel"),
        ],
    )
    def test_5xx_e_429_seguem_o_caminho_transitorio_sem_o_log(
        self,
        gateway: MercadoPagoGateway,
        log: LogDoAdaptador,
        operacao: str,
        status: int,
    ) -> None:
        metodo, caminho, chamar = OPERACOES[operacao]
        with respx.mock(base_url=API) as mp:
            mp.route(method=metodo, path=caminho).mock(
                return_value=erro_mp(status, "x")
            )
            with pytest.raises(GatewayPagamentoIndisponivelError):
                chamar(gateway)

        assert log.recusas() == []
        assert log.avisos() == []

    @pytest.mark.parametrize(
        ("cause", "esperado"),
        [
            pytest.param(
                [{"code": "4001"}, {"code": "x_y"}], ["4001", "x_y"], id="dois-textos"
            ),
            pytest.param([{"code": 2000}], ["2000"], id="inteiro-vira-texto"),
            pytest.param([{"code": "a.b-c_D9"}], ["a.b-c_D9"], id="alfabeto-inteiro"),
            pytest.param([{"code": "x" * 40}], ["x" * 40], id="quarenta"),
            pytest.param([{"code": "x" * 41}], [], id="quarenta-e-um"),
            pytest.param([{"code": "4001\n"}], [], id="quebra-de-linha-no-fim"),
            pytest.param([{"code": "\n4001"}], [], id="quebra-de-linha-no-comeco"),
            pytest.param(
                [{"code": "4001 ok"}, {"code": "a/b"}, {"code": "ação"}, {"code": ""}],
                [],
                id="fora-do-alfabeto",
            ),
            pytest.param(
                [{"code": True}, {"code": 2.5}, {"code": None}, {"code": ["a"]}],
                [],
                id="tipos-que-nao-sao-codigo",
            ),
            pytest.param([{"description": "sem code"}, {}], [], id="sem-code"),
            pytest.param(
                ["4001", 4001, None, [{"code": "4001"}], True],
                [],
                id="itens-que-nao-sao-objeto",
            ),
            pytest.param(
                [{"code": "ok1"}, {"code": "ruim!"}, {"code": "ok2"}],
                ["ok1", "ok2"],
                id="o-invalido-sai-e-o-resto-fica",
            ),
            pytest.param(
                [{"code": "a"}, {"code": "a"}, {"code": 4001}, {"code": "4001"}],
                ["a", "a", "4001", "4001"],
                id="codigo-repetido-fica-na-ordem",
            ),
            pytest.param({"code": "4001"}, [], id="cause-objeto"),
            pytest.param("4001", [], id="cause-texto"),
            pytest.param(None, [], id="cause-nula"),
            pytest.param(5, [], id="cause-numero"),
        ],
    )
    def test_causas_so_com_os_codigos_no_formato_e_nunca_levanta(
        self,
        gateway: MercadoPagoGateway,
        log: LogDoAdaptador,
        cause: object,
        esperado: list[str],
    ) -> None:
        corpo = {"error": "bad_request", "cause": cause}

        recusar(gateway, "criar_cobranca", httpx.Response(400, json=corpo))

        assert uma_recusa(log)["causas"] == esperado

    def test_no_maximo_dez_causas_as_primeiras_na_ordem(
        self, gateway: MercadoPagoGateway, log: LogDoAdaptador
    ) -> None:
        causas = [{"code": f"c{n}"} for n in range(11)]

        recusar(gateway, "criar_cobranca", httpx.Response(400, json={"cause": causas}))

        assert uma_recusa(log)["causas"] == [f"c{n}" for n in range(10)]

    def test_dez_causas_cabem_todas(
        self, gateway: MercadoPagoGateway, log: LogDoAdaptador
    ) -> None:
        causas = [{"code": f"c{n}"} for n in range(10)]

        recusar(gateway, "criar_cobranca", httpx.Response(400, json={"cause": causas}))

        assert uma_recusa(log)["causas"] == [f"c{n}" for n in range(10)]

    def test_o_teto_conta_so_os_codigos_validos(
        self, gateway: MercadoPagoGateway, log: LogDoAdaptador
    ) -> None:
        invalidas = [{"code": "ruim!"}, {"descricao": "sem code"}, "lixo"]
        validas = [{"code": f"c{n}"} for n in range(12)]

        recusar(
            gateway,
            "criar_cobranca",
            httpx.Response(400, json={"cause": invalidas + validas}),
        )

        assert uma_recusa(log)["causas"] == [f"c{n}" for n in range(10)]

    @pytest.mark.parametrize(
        "erro",
        [
            pytest.param("bad_request", id="bad-request"),
            pytest.param("not_found", id="not-found"),
            pytest.param("a", id="uma-letra"),
            pytest.param("_", id="sublinhado"),
            pytest.param("a" * 40, id="quarenta"),
        ],
    )
    def test_erro_no_formato_sai_como_veio(
        self, gateway: MercadoPagoGateway, log: LogDoAdaptador, erro: str
    ) -> None:
        recusar(gateway, "criar_cobranca", httpx.Response(400, json={"error": erro}))

        assert uma_recusa(log)["erro"] == erro

    @pytest.mark.parametrize(
        "erro",
        [
            pytest.param("Bad_Request", id="maiuscula"),
            pytest.param("bad-request", id="hifen"),
            pytest.param("bad request", id="espaco"),
            pytest.param("bad_request1", id="digito"),
            pytest.param("bad_request\n", id="quebra-de-linha-no-fim"),
            pytest.param("ação", id="acento"),
            pytest.param("", id="vazio"),
            pytest.param("a" * 41, id="quarenta-e-um"),
            pytest.param(400, id="inteiro"),
            pytest.param(True, id="booleano"),
            pytest.param(None, id="nulo"),
            pytest.param(["bad_request"], id="lista"),
        ],
    )
    def test_erro_fora_do_formato_fica_de_fora(
        self, gateway: MercadoPagoGateway, log: LogDoAdaptador, erro: object
    ) -> None:
        recusar(gateway, "criar_cobranca", httpx.Response(400, json={"error": erro}))

        assert "erro" not in uma_recusa(log)

    def test_corpo_sem_error_nem_cause_loga_so_operacao_e_status(
        self, gateway: MercadoPagoGateway, log: LogDoAdaptador
    ) -> None:
        recusa = recusar(
            gateway, "criar_cobranca", httpx.Response(400, json={"message": "x"})
        )

        assert uma_recusa(log) == {
            "operacao": "criar_cobranca",
            "status_http": 400,
            "causas": [],
        }
        assert str(recusa) == "Provedor de pagamento recusou a operacao"

    @pytest.mark.parametrize(
        "corpo",
        [
            pytest.param({"text": "<html>Bad Request</html>"}, id="html"),
            pytest.param({"content": b""}, id="vazio"),
            pytest.param({"content": b"\xff\xfe\xfa"}, id="bytes-que-nao-sao-texto"),
            pytest.param({"content": b"null"}, id="nulo"),
            pytest.param({"json": 400}, id="numero"),
            pytest.param({"json": "bad_request"}, id="texto"),
            pytest.param(
                {"json": [{"error": "bad_request", "cause": [{"code": "4001"}]}]},
                id="lista",
            ),
        ],
    )
    def test_corpo_que_nao_e_um_objeto_json_nao_tem_erro_nem_causas(
        self, gateway: MercadoPagoGateway, log: LogDoAdaptador, corpo: dict[str, Any]
    ) -> None:
        recusa = recusar(gateway, "criar_cobranca", httpx.Response(400, **corpo))

        assert uma_recusa(log) == {
            "operacao": "criar_cobranca",
            "status_http": 400,
            "causas": [],
        }
        assert str(recusa) == "Provedor de pagamento recusou a operacao"

    def test_repr_da_recusa_nao_traz_o_texto_do_provedor(self) -> None:
        # Qualquer repr, `%r` ou traceback com locals imprimiria a mensagem.
        recusa = mercadopago._ler_recusa(erro_mp(400, "SENTINELA-MSG payer x@y.com"))

        assert (
            recusa.mensagem == "Mercado Pago respondeu 400: SENTINELA-MSG payer x@y.com"
        )
        assert (
            repr(recusa) == "_Recusa(status=400, erro='bad_request', causas=('2000',))"
        )

    def test_codigo_com_cara_de_cpf_ou_telefone_sai_mascarado_pelo_scrub(
        self, gateway: MercadoPagoGateway, log: LogDoAdaptador
    ) -> None:
        causas = [{"code": c} for c in ("123.456.789-09", "11-99999-0000", "ok_1")]

        recusar(gateway, "criar_cobranca", httpx.Response(400, json={"cause": causas}))

        assert uma_recusa(log)["causas"] == ["***.***.789-**", "***", "ok_1"]
        assert "123.456" not in log.texto


class TestLogDoStatusDesconhecido:
    """Status de pagamento que o mapa nao conhece: a cobranca nao muda, e o log traz o
    status so se ele tiver o formato do vocabulario do provedor."""

    def consultar(
        self, gateway: MercadoPagoGateway, status: str
    ) -> SituacaoNoProvedor | None:
        with respx.mock(base_url=API) as mp:
            mp.get("/v1/payments/1").respond(
                200, json=pagamento_no_provedor(status=status)
            )
            return gateway.consultar_pagamento("1")

    @pytest.mark.parametrize(
        "status",
        [
            pytest.param("novo_status", id="novo-status"),
            pytest.param("a", id="uma-letra"),
            pytest.param("_", id="sublinhado"),
            pytest.param("a" * 40, id="quarenta"),
        ],
    )
    def test_status_no_formato_sai_no_log(
        self, gateway: MercadoPagoGateway, log: LogDoAdaptador, status: str
    ) -> None:
        situacao = self.consultar(gateway, status)

        assert situacao is not None
        assert situacao.status is StatusNoProvedor.EM_ANDAMENTO
        [linha] = log.eventos("mercadopago_unknown_status")
        assert linha["level"] == "warning"
        assert campos_do_evento(linha) == {"status": status}

    @pytest.mark.parametrize(
        "status",
        [
            pytest.param("Novo Status", id="maiuscula-e-espaco"),
            pytest.param("novo-status", id="hifen"),
            pytest.param("novo_status2", id="digito"),
            pytest.param("novo_status\n", id="quebra-de-linha-no-fim"),
            pytest.param("a" * 41, id="quarenta-e-um"),
            pytest.param("ação", id="acento"),
            pytest.param("SENTINELA payer x@y.com", id="texto-livre"),
        ],
    )
    def test_status_fora_do_formato_sai_sem_o_campo_e_e_contado(
        self, gateway: MercadoPagoGateway, log: LogDoAdaptador, status: str
    ) -> None:
        antes = contador("consultar_pagamento", "status_desconhecido")

        situacao = self.consultar(gateway, status)

        assert situacao is not None
        assert situacao.status is StatusNoProvedor.EM_ANDAMENTO
        [linha] = log.eventos("mercadopago_unknown_status")
        assert campos_do_evento(linha) == {}
        assert contador("consultar_pagamento", "status_desconhecido") == antes + 1


class TestCircuitBreaker:
    def test_circuito_aberto_recusa_sem_chamar_o_provedor(
        self, esperas: Esperas
    ) -> None:
        gateway = MercadoPagoGateway(
            ConfiguracaoMercadoPago(
                access_token=TOKEN, notification_url=NOTIFICACAO, tentativas=1
            ),
            breaker=CircuitBreaker(
                "mercadopago-cb", falha=FalhaTransitoriaError, limite_falhas=2
            ),
            dormir=esperas,
        )
        try:
            with respx.mock(base_url=API) as mp:
                rota = mp.get("/v1/payments/1").respond(502)
                for _ in range(2):
                    with pytest.raises(GatewayPagamentoIndisponivelError):
                        gateway.consultar_pagamento("1")
                antes = contador("consultar_pagamento", "circuito_aberto")
                with pytest.raises(GatewayPagamentoIndisponivelError):
                    gateway.consultar_pagamento("1")
            assert rota.call_count == 2
            assert contador("consultar_pagamento", "circuito_aberto") == antes + 1
        finally:
            gateway.fechar()

    def test_configuracao_nao_expoe_o_token_no_repr(self) -> None:
        assert TOKEN not in repr(ConfiguracaoMercadoPago(TOKEN, NOTIFICACAO))

    def test_disjuntor_padrao_abre_na_quinta_falha_seguida(
        self, esperas: Esperas
    ) -> None:
        assert (FALHAS_PARA_ABRIR, SEGUNDOS_ABERTO) == (5, 30.0)
        gateway = MercadoPagoGateway(
            ConfiguracaoMercadoPago(
                access_token=TOKEN, notification_url=NOTIFICACAO, tentativas=1
            ),
            dormir=esperas,
        )
        try:
            with respx.mock(base_url=API) as mp:
                rota = mp.get("/v1/payments/1").respond(503)
                for _ in range(FALHAS_PARA_ABRIR + 1):
                    with pytest.raises(GatewayPagamentoIndisponivelError):
                        gateway.consultar_pagamento("1")
            assert rota.call_count == FALHAS_PARA_ABRIR
        finally:
            gateway.fechar()

    @pytest.mark.parametrize("operacao", ["criar_cobranca", "estornar"])
    def test_disjuntor_vale_tambem_para_os_post(
        self, esperas: Esperas, operacao: str
    ) -> None:
        gateway = MercadoPagoGateway(
            ConfiguracaoMercadoPago(
                access_token=TOKEN, notification_url=NOTIFICACAO, tentativas=1
            ),
            breaker=CircuitBreaker(
                f"mp-post-{operacao}", falha=FalhaTransitoriaError, limite_falhas=2
            ),
            dormir=esperas,
        )
        caminho = (
            "/checkout/preferences"
            if operacao == "criar_cobranca"
            else "/v1/payments/1/refunds"
        )

        def chamar() -> None:
            if operacao == "criar_cobranca":
                gateway.criar_cobranca(
                    pagamento_id=uuid4(),
                    itens=[ItemCobranca("SRV-X", "X", 1, dinheiro("1.00"))],
                    expira_em=AGORA,
                )
            else:
                gateway.estornar("1", chave_idempotencia="k")

        try:
            with respx.mock(base_url=API) as mp:
                rota = mp.post(caminho).respond(502)
                for _ in range(3):
                    with pytest.raises(GatewayPagamentoIndisponivelError):
                        chamar()
            assert rota.call_count == 2  # a terceira nem sai daqui
        finally:
            gateway.fechar()

    def test_timeout_do_cliente_vem_da_configuracao(self) -> None:
        gateway = MercadoPagoGateway(
            ConfiguracaoMercadoPago(
                access_token=TOKEN, notification_url=NOTIFICACAO, timeout_segundos=2.5
            )
        )
        try:
            with respx.mock(base_url=API) as mp:
                rota = mp.get("/v1/payments/1").respond(
                    200, json=pagamento_no_provedor()
                )
                gateway.consultar_pagamento("1")
            assert rota.calls.last.request.extensions["timeout"] == dict.fromkeys(
                ("connect", "read", "write", "pool"), 2.5
            )
        finally:
            gateway.fechar()


def test_numero_json_recusa_valor_sem_representacao_exata() -> None:
    assert repr(numero_json(Decimal("120.35"))) == "120.35"
    inexato = Decimal("12345678901234567.89")
    with pytest.raises(ValueError, match="exata"):
        numero_json(inexato)


class TestAssinaturaDoWebhook:
    SEGREDO = "segredo-do-webhook"

    def assinar(self, manifesto: str) -> str:
        return hmac.new(
            self.SEGREDO.encode(), manifesto.encode(), hashlib.sha256
        ).hexdigest()

    def test_manifesto_completo_da_documentacao(self) -> None:
        # Digest conferido fora do codigo (openssl dgst -sha256 -hmac) para o
        # manifesto "id:123456;request-id:bb56a2f1-6aae-46ac-982e;ts:1742505638683;".
        v1 = "a8d555d2fd37ff9d161cedbdc6d6fca83c39390f62befb4d8f771b070eaaa6ac"
        assert assinatura_webhook_valida(
            segredo=self.SEGREDO,
            x_signature=f"ts=1742505638683,v1={v1}",
            x_request_id="bb56a2f1-6aae-46ac-982e",
            data_id="123456",
        )

    def test_data_id_alfanumerico_entra_em_minusculas(self) -> None:
        v1 = self.assinar("id:abc123;request-id:r1;ts:1;")
        assert assinatura_webhook_valida(
            segredo=self.SEGREDO,
            x_signature=f"ts=1, v1={v1}",
            x_request_id="r1",
            data_id="ABC123",
        )

    def test_parte_ausente_sai_do_manifesto(self) -> None:
        v1 = self.assinar("id:123;ts:1;")
        assert assinatura_webhook_valida(
            segredo=self.SEGREDO,
            x_signature=f"ts=1,v1={v1}",
            x_request_id=None,
            data_id="123",
        )
        so_ts = self.assinar("request-id:r1;ts:1;")
        assert assinatura_webhook_valida(
            segredo=self.SEGREDO,
            x_signature=f"ts=1,v1={so_ts}",
            x_request_id="r1",
            data_id=None,
        )

    @pytest.mark.parametrize(
        ("x_signature", "data_id"),
        [
            (None, "123"),
            ("", "123"),
            ("v1=abc", "123"),
            ("ts=1", "123"),
            ("ts=1,v1=00", "123"),
            ("lixo", "123"),
            ("ts=1,v1=çãõ", "123"),
        ],
        ids=[
            "sem-cabecalho",
            "vazio",
            "sem-ts",
            "sem-v1",
            "v1-errado",
            "lixo",
            "nao-ascii",
        ],
    )
    def test_assinatura_ausente_ou_errada(
        self, x_signature: str | None, data_id: str
    ) -> None:
        assert not assinatura_webhook_valida(
            segredo=self.SEGREDO,
            x_signature=x_signature,
            x_request_id="r1",
            data_id=data_id,
        )

    def test_assinatura_de_outro_pagamento_nao_vale(self) -> None:
        v1 = self.assinar("id:111;request-id:r1;ts:1;")
        assert not assinatura_webhook_valida(
            segredo=self.SEGREDO,
            x_signature=f"ts=1,v1={v1}",
            x_request_id="r1",
            data_id="222",
        )

    def test_sem_segredo_nada_e_valido(self) -> None:
        vazio = b""
        v1 = hmac.new(vazio, b"id:1;request-id:r1;ts:1;", hashlib.sha256).hexdigest()
        assert not assinatura_webhook_valida(
            segredo="", x_signature=f"ts=1,v1={v1}", x_request_id="r1", data_id="1"
        )


def test_recusa_nao_transporta_texto_do_provedor() -> None:
    exc = GatewayPagamentoRecusouError("SENTINELA_PRIVACIDADE")
    assert str(exc) == exc.mensagem == "Provedor de pagamento recusou a operacao"
    assert exc.args == ("Provedor de pagamento recusou a operacao",)
    assert exc.codigo == "GATEWAY_PAGAMENTO_RECUSOU"
