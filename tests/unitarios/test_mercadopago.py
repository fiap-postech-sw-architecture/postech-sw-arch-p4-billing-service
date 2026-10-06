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
import json
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any
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
from src.pagamento.dominio.estados import StatusNoProvedor
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
            with pytest.raises(GatewayPagamentoRecusouError, match="sem detalhe"):
                gateway.estornar("1", chave_idempotencia="msg-4")

    def test_referencia_invalida_e_recusada_sem_chamar(
        self, gateway: MercadoPagoGateway
    ) -> None:
        with pytest.raises(GatewayPagamentoRecusouError, match="invalida"):
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
        with pytest.raises(GatewayPagamentoRecusouError, match="invalida"):
            gateway.cancelar_cobranca("../preferences")


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
        with respx.mock(base_url=API) as mp:
            rota = mp.post("/checkout/preferences").respond(201, json=corpo)
            with pytest.raises(GatewayPagamentoIndisponivelError):
                gateway.criar_cobranca(
                    pagamento_id=uuid4(),
                    itens=[ItemCobranca("SRV-X", "X", 1, dinheiro("1.00"))],
                    expira_em=AGORA,
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
            with pytest.raises(GatewayPagamentoRecusouError, match="not found"):
                gateway.cancelar_cobranca("pref-1")
        assert contador("cancelar_cobranca", "nao_encontrado") == antes + 1


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
    with pytest.raises(ValueError, match="exata"):
        numero_json(Decimal("12345678901234567.89"))


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
