"""Teste de contrato do adapter do Mercado Pago (HTTP mockado com respx).

Payloads no formato da documentacao oficial do Mercado Pago:

- Criar preferencia (Checkout Pro): https://www.mercadopago.com.br/developers/pt/reference/online-payments/checkout-pro-preferences/create-preference/post
- Obter pagamento: https://www.mercadopago.com.br/developers/pt/reference/online-payments/checkout-api-payments/get-payment/get
- Criar reembolso: https://www.mercadopago.com.br/developers/pt/reference/online-payments/checkout-api-payments/create-refund/post
- Notificacoes (x-signature): https://www.mercadopago.com.br/developers/pt/docs/checkout-pro-preferences/payment-notifications
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import respx
from prometheus_client import REGISTRY

from src.compartilhado.infraestrutura.circuit_breaker import CircuitBreaker
from src.pagamento.aplicacao.ports import (
    EstornoEmProcessamentoError,
    GatewayPagamentoIndisponivelError,
    GatewayPagamentoRecusouError,
    ItemCobranca,
)
from src.pagamento.dominio.pagamento import StatusPagamento
from src.pagamento.infraestrutura.mercadopago import (
    ConfiguracaoMercadoPago,
    MercadoPagoGateway,
    _FalhaTransitoriaError,
    _numero_json,
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
def gateway(esperas: Esperas) -> MercadoPagoGateway:
    gateway = MercadoPagoGateway(
        ConfiguracaoMercadoPago(access_token=TOKEN, notification_url=NOTIFICACAO),
        breaker=CircuitBreaker("mercadopago-teste", falha=_FalhaTransitoriaError),
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

    def test_nao_repete_porque_criar_preferencia_nao_e_idempotente(
        self, gateway: MercadoPagoGateway, esperas: Esperas
    ) -> None:
        with respx.mock(base_url=API) as mp:
            rota = mp.post("/checkout/preferences").respond(503)
            with pytest.raises(GatewayPagamentoIndisponivelError):
                gateway.criar_cobranca(
                    pagamento_id=uuid4(),
                    itens=[ItemCobranca("SRV-X", "X", 1, dinheiro("1.00"))],
                    expira_em=AGORA,
                )
        assert rota.call_count == 1
        assert esperas == []

    def test_erro_400_vira_recusa_com_a_mensagem_do_provedor(
        self, gateway: MercadoPagoGateway
    ) -> None:
        with respx.mock(base_url=API) as mp:
            mp.post("/checkout/preferences").mock(
                return_value=erro_mp(400, "unit_price invalid")
            )
            with pytest.raises(
                GatewayPagamentoRecusouError, match="unit_price invalid"
            ):
                gateway.criar_cobranca(
                    pagamento_id=uuid4(),
                    itens=[ItemCobranca("SRV-X", "X", 1, dinheiro("1.00"))],
                    expira_em=AGORA,
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
        assert situacao.status is StatusPagamento.APROVADO
        assert situacao.status_provedor == "approved"
        assert situacao.detalhe == "accredited"
        assert situacao.valor == dinheiro("335.00")
        assert contador("consultar_pagamento", "sucesso") == antes + 1

    @pytest.mark.parametrize("valor", [335.0, "335.00", 335])
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
            # Tentativa recusada nao encerra a cobranca do Checkout Pro: o
            # comprador pode pagar de novo ate o prazo.
            ("rejected", StatusPagamento.PENDENTE),
            ("cancelled", StatusPagamento.PENDENTE),
            ("refunded", StatusPagamento.ESTORNADO),
            ("charged_back", StatusPagamento.ESTORNADO),
            ("pending", StatusPagamento.PENDENTE),
            ("in_process", StatusPagamento.PENDENTE),
        ],
    )
    def test_mapa_de_status(
        self, gateway: MercadoPagoGateway, status: str, esperado: StatusPagamento
    ) -> None:
        with respx.mock(base_url=API) as mp:
            mp.get("/v1/payments/1").respond(
                200, json=pagamento_no_provedor(status=status)
            )
            situacao = gateway.consultar_pagamento("1")
        assert situacao is not None
        assert situacao.status is esperado
        assert situacao.status_provedor == status

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
            with pytest.raises(GatewayPagamentoRecusouError, match="already refunded"):
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
            with pytest.raises(GatewayPagamentoRecusouError, match=status):
                gateway.estornar("1", chave_idempotencia="msg-6")

    @pytest.mark.parametrize("corpo", ["", "[]", '{"id": 1}'])
    def test_estorno_sem_status_nao_e_dado_como_concluido(
        self, gateway: MercadoPagoGateway, corpo: str
    ) -> None:
        with respx.mock(base_url=API) as mp:
            mp.post("/v1/payments/1/refunds").respond(201, text=corpo)
            with pytest.raises(EstornoEmProcessamentoError):
                gateway.estornar("1", chave_idempotencia="msg-7")

    def test_erro_sem_corpo_json(self, gateway: MercadoPagoGateway) -> None:
        with respx.mock(base_url=API) as mp:
            mp.post("/v1/payments/1/refunds").respond(404, text="not found")
            with pytest.raises(GatewayPagamentoRecusouError, match="sem detalhe"):
                gateway.estornar("1", chave_idempotencia="msg-4")

    def test_referencia_invalida_e_recusada_sem_chamar(
        self, gateway: MercadoPagoGateway
    ) -> None:
        with pytest.raises(GatewayPagamentoRecusouError, match="invalida"):
            gateway.estornar("../1", chave_idempotencia="msg")


class TestCircuitBreaker:
    def test_circuito_aberto_recusa_sem_chamar_o_provedor(
        self, esperas: Esperas
    ) -> None:
        gateway = MercadoPagoGateway(
            ConfiguracaoMercadoPago(
                access_token=TOKEN, notification_url=NOTIFICACAO, tentativas=1
            ),
            breaker=CircuitBreaker(
                "mercadopago-cb", falha=_FalhaTransitoriaError, limite_falhas=2
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

    def test_gateway_sem_breaker_injetado_cria_o_proprio(self) -> None:
        gateway = MercadoPagoGateway(
            ConfiguracaoMercadoPago(access_token=TOKEN, notification_url=NOTIFICACAO)
        )
        gateway.fechar()
        assert gateway.provedor == "mercadopago"


def test_numero_json_recusa_valor_sem_representacao_exata() -> None:
    assert repr(_numero_json(Decimal("120.35"))) == "120.35"
    with pytest.raises(ValueError, match="exata"):
        _numero_json(Decimal("12345678901234567.89"))


class TestAssinaturaDoWebhook:
    SEGREDO = "segredo-do-webhook"

    def assinar(self, manifesto: str) -> str:
        return hmac.new(
            self.SEGREDO.encode(), manifesto.encode(), hashlib.sha256
        ).hexdigest()

    def test_manifesto_completo_da_documentacao(self) -> None:
        v1 = self.assinar(
            "id:123456;request-id:bb56a2f1-6aae-46ac-982e;ts:1742505638683;"
        )
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
