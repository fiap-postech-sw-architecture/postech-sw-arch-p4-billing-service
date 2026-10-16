"""O texto livre da recusa do Mercado Pago nao sai nos logs por onde a recusa passa.

Cada teste faz o adaptador real (httpx com respx) receber uma recusa 4xx cujo corpo
traz um texto sentinela, e-mail e token de aparencia real, e percorre um caminho que
trata a recusa: o consumidor (DLQ e estorno), o ``prazos`` (conciliacao e estorno
automatico) e a API (webhook). A saida inteira e conferida: o JSON do pipeline de log
de producao (com o scrub de PII), stdout e stderr, o registro cru e o traceback do
``caplog``, os spans e as metricas.
"""

from __future__ import annotations

import io
import json
import logging
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx
import structlog
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from pymongo.errors import OperationFailure

from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    ConsumidorDeComandos,
)
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.consumidor import FILA, criar_handlers
from src.main import criar_app
from src.pagamento.dominio.pagamento import Pagamento
from src.pagamento.infraestrutura.mercadopago import (
    ConfiguracaoMercadoPago,
    MercadoPagoGateway,
)
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository
from src.prazos import executar_ciclo
from src.seed import semear
from tests.factories import AGORA, pagamento
from tests.integracao.apoio import (
    CanalDeTeste,
    GatewayRoteirizado,
    MetricasEspia,
    RelogioFixo,
    comando,
    configuracao,
    entregar,
    provedor_descartavel,
)
from tests.integracao.test_api_pagamentos import WEBHOOK, assinatura
from tests.integracao.test_comandos_da_saga import (
    _orcamento_id,
    _pagamento,
    aprovar_pelo_atendente,
    gerar,
    pagar_no_checkout,
    solicitar,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from pymongo.database import Database

    Banco = Database[dict[str, Any]]

BASE = "https://api.mercadopago.com.teste"
EMAIL = "maria.souza1987@example.com"
# Token no formato do Mercado Pago, montado aqui para nao ser um literal.
CREDENCIAL = "-".join(
    (
        "APP_USR",
        "3128571255318271",
        "101411",
        "5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b",
        "123456789",
    )
)
# Pedacos do texto do provedor: o que nao deve aparecer em saida nenhuma.
MARCADORES = ("SENTINELA", "maria.souza", "APP_USR", "3128571255318271", "payer")


def recusa(status: int = 400) -> httpx.Response:
    """Recusa do provedor com texto livre em todo campo que o corpo costuma ter."""
    corpo = {
        "message": f"SENTINELA-MSG payer {EMAIL} token {CREDENCIAL}",
        "error": "bad_request",
        "status": status,
        "cause": [
            {
                "code": "4001",
                "description": f"SENTINELA-DESC {EMAIL} {CREDENCIAL}",
                "message": "SENTINELA-CMSG",
                "data": "SENTINELA-CDATA",
            },
            {"code": "x_y", "description": "SENTINELA-DESC2"},
        ],
        "detail": "SENTINELA-DETAIL",
    }
    return httpx.Response(status, json=corpo)


class Captura:
    """Tudo o que o processo deixou: o que o operador e o Jaeger veem."""

    def __init__(
        self,
        saida: io.StringIO,
        capfd: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
        spans: InMemorySpanExporter,
    ) -> None:
        self._saida = saida
        self._capfd = capfd
        self._caplog = caplog
        self._spans = spans
        self._stdout = ""
        self._stderr = ""

    def _ler_stdout_e_stderr(self) -> None:
        # O capfd entrega o que chegou desde a ultima leitura: acumula.
        lido = self._capfd.readouterr()
        self._stdout += lido.out
        self._stderr += lido.err

    def linhas(self) -> list[dict[str, Any]]:
        """Linhas JSON do pipeline de log (a API reconfigura o log para o stdout)."""
        self._ler_stdout_e_stderr()
        linhas = []
        for texto in (self._saida.getvalue() + "\n" + self._stdout).splitlines():
            try:
                linha = json.loads(texto)
            except ValueError:
                continue
            if isinstance(linha, dict):
                linhas.append(linha)
        return linhas

    def eventos(self, nome: str) -> list[dict[str, Any]]:
        return [linha for linha in self.linhas() if linha.get("event") == nome]

    def saidas(self) -> dict[str, str]:
        self._ler_stdout_e_stderr()
        formatador = logging.Formatter()
        registros = "\n".join(
            f"{r.__dict__!r}\n{formatador.format(r)}" for r in self._caplog.records
        )
        spans = "\n".join(
            f"{s.name} {dict(s.attributes or {})} {s.status.description} "
            f"{[(e.name, dict(e.attributes or {})) for e in s.events]} {s.links}"
            for s in self._spans.get_finished_spans()
        )
        return {
            "json-do-pipeline": self._saida.getvalue(),
            "stdout": self._stdout,
            "stderr": self._stderr,
            "caplog": registros,
            "spans": spans,
            "metricas": generate_latest().decode(),
        }

    def vazamentos(self) -> dict[str, list[str]]:
        """Em qual saida aparece qual pedaco do texto do provedor."""
        achados: dict[str, list[str]] = {}
        for nome, texto in self.saidas().items():
            achou = [m for m in MARCADORES if m in texto]
            if achou:
                achados[nome] = achou
        return achados


@pytest.fixture
def captura(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    spans: InMemorySpanExporter,
) -> Iterator[Captura]:
    """``configurar_logging`` de producao numa saida de teste, com os loggers dos
    modulos religados: o structlog guarda a configuracao de quando o logger foi
    usado pela primeira vez."""
    from src.compartilhado.infraestrutura import circuit_breaker
    from src.compartilhado.interfaces import error_handler
    from src.pagamento.infraestrutura import mercadopago
    from src.pagamento.interfaces import router

    raiz = logging.getLogger()
    # Os handlers do pytest por fase: ele mesmo os poe e os tira.
    do_pytest = type(caplog.handler)
    handlers, nivel_da_raiz = raiz.handlers[:], raiz.level
    configuracao_antes = structlog.get_config()
    saida = io.StringIO()
    configurar_logging(saida)
    json_do_pipeline = raiz.handlers[0]
    # O pipeline na frente; os handlers do pytest seguem no lugar.
    raiz.handlers = [json_do_pipeline, *handlers]
    for modulo, nome in (
        (mercadopago, "_log"),
        (error_handler, "logger"),
        (router, "_log"),
        (circuit_breaker, "_log"),
    ):
        monkeypatch.setattr(modulo, nome, structlog.get_logger(modulo.__name__))
    yield Captura(saida, capfd, caplog, spans)
    structlog.configure(**configuracao_antes)
    # A API (criar_app) troca os handlers da raiz: volta o que havia antes, mais os
    # do pytest desta fase.
    raiz.handlers = [
        *(h for h in handlers if not isinstance(h, do_pytest)),
        *(h for h in raiz.handlers if isinstance(h, do_pytest)),
    ]
    raiz.setLevel(nivel_da_raiz)


@pytest.fixture
def mp() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as roteador:
        yield roteador


@pytest.fixture
def real() -> Iterator[MercadoPagoGateway]:
    gateway = MercadoPagoGateway(
        ConfiguracaoMercadoPago(
            access_token="TEST-0000000000000000-000000-00000000000000000000000000000000-0",
            notification_url="https://hook.teste/api/v1/webhooks/mercadopago",
            base_url=BASE,
            timeout_segundos=2.0,
        ),
        dormir=lambda _segundos: None,
    )
    yield gateway
    gateway.fechar()


@pytest.fixture
def relogio() -> RelogioFixo:
    return RelogioFixo()


def consumidor_com(
    banco: Banco, gateway: MercadoPagoGateway | GatewayRoteirizado, relogio: RelogioFixo
) -> ConsumidorDeComandos:
    handlers = criar_handlers(configuracao().comandos, gateway=gateway, relogio=relogio)
    return ConsumidorDeComandos(
        banco, handlers, fila=FILA, usuario="billing", relogio=relogio
    )


def orcamento_aprovado(
    banco: Banco, relogio: RelogioFixo, gateway: MercadoPagoGateway | GatewayRoteirizado
) -> tuple[UUID, UUID, ConsumidorDeComandos, CanalDeTeste]:
    semear(banco)
    canal = CanalDeTeste()
    consumidor = consumidor_com(banco, gateway, relogio)
    ordem_id = uuid4()
    entregar(consumidor, canal, gerar(ordem_id))
    aprovar_pelo_atendente(banco, relogio, ordem_id)
    return ordem_id, _orcamento_id(banco, ordem_id), consumidor, canal


def pagamento_confirmado(
    banco: Banco, relogio: RelogioFixo
) -> tuple[UUID, dict[str, Any]]:
    """Ordem com o pagamento CONFIRMADO no provedor simulado (referencia ``sim-...``);
    depois o teste troca para o adaptador real."""
    simulado = GatewayRoteirizado()
    ordem_id, orcamento_id, consumidor, canal = orcamento_aprovado(
        banco, relogio, simulado
    )
    entregar(consumidor, canal, solicitar(ordem_id, orcamento_id))
    pagar_no_checkout(banco, simulado, relogio, ordem_id)
    return ordem_id, _pagamento(banco, ordem_id)


def consulta_aprovada(mp: respx.MockRouter, p: dict[str, Any]) -> None:
    mp.get(url__regex=r"/v1/payments/sim-[0-9a-f]+$").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": p["referencia_pagamento"],
                "status": "approved",
                "external_reference": str(p["_id"]),
                "transaction_amount": 335.0,
                "currency_id": "BRL",
            },
        )
    )


def salvar_solicitado(banco: Banco) -> Pagamento:
    p = pagamento()
    uow = MongoUnitOfWork(banco)
    uow.executar(partial(MongoPagamentoRepository(uow).salvar, p))
    return p


def aprovacao_de_valor_diferente(mp: respx.MockRouter, p: Pagamento) -> None:
    """A busca acha a tentativa 777 aprovada por outro valor: estorno automatico."""
    mp.get("/v1/payments/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 777,
                        "status": "approved",
                        "external_reference": str(p.id),
                        "transaction_amount": 1.0,
                        "currency_id": "BRL",
                    }
                ]
            },
        )
    )


def ciclo_do_prazos(banco: Banco, real: MercadoPagoGateway) -> None:
    executar_ciclo(
        banco,
        gateway=real,
        metricas=MetricasEspia(),
        max_recusas=3,
        relogio=RelogioFixo(AGORA),
    )


class TestConsumidor:
    def test_criar_cobranca_recusada_vai_para_a_dlq_so_com_o_codigo(
        self,
        banco: Banco,
        relogio: RelogioFixo,
        real: MercadoPagoGateway,
        mp: respx.MockRouter,
        captura: Captura,
    ) -> None:
        ordem_id, orcamento_id, consumidor, canal = orcamento_aprovado(
            banco, relogio, real
        )
        mp.post("/checkout/preferences").mock(return_value=recusa())

        resultado = entregar(consumidor, canal, solicitar(ordem_id, orcamento_id))

        assert resultado == "dlq"
        [rejeicao] = captura.eventos("mercadopago_request_rejected")
        assert (rejeicao["operacao"], rejeicao["status_http"]) == (
            "criar_cobranca",
            400,
        )
        [dlq] = captura.eventos("command_dead_lettered")
        assert dlq["codigo"] == "GATEWAY_PAGAMENTO_RECUSOU"
        assert captura.vazamentos() == {}

    def test_fechar_o_checkout_recusado_loga_so_o_codigo_e_a_compensacao_segue(
        self,
        banco: Banco,
        relogio: RelogioFixo,
        real: MercadoPagoGateway,
        mp: respx.MockRouter,
        captura: Captura,
    ) -> None:
        simulado = GatewayRoteirizado()
        ordem_id, orcamento_id, consumidor, canal = orcamento_aprovado(
            banco, relogio, simulado
        )
        entregar(consumidor, canal, solicitar(ordem_id, orcamento_id))
        mp.put(url__regex=r"/checkout/preferences/.+").mock(return_value=recusa())

        resultado = entregar(
            consumidor_com(banco, real, relogio),
            canal,
            comando(
                "EstornarPagamento",
                {"ordem_id": str(ordem_id), "motivo": "cancelamento"},
            ),
        )

        assert resultado == "processada"
        [rejeicao] = captura.eventos("mercadopago_request_rejected")
        assert rejeicao["operacao"] == "cancelar_cobranca"
        [fechar] = captura.eventos("checkout_close_refused")
        assert fechar["codigo"] == "GATEWAY_PAGAMENTO_RECUSOU"
        assert fechar["pagamento_id"] == str(_pagamento(banco, ordem_id)["_id"])
        assert "erro" not in fechar
        assert captura.vazamentos() == {}

    def test_estorno_recusado_loga_refund_refused_so_com_o_pagamento(
        self,
        banco: Banco,
        relogio: RelogioFixo,
        real: MercadoPagoGateway,
        mp: respx.MockRouter,
        captura: Captura,
    ) -> None:
        ordem_id, p = pagamento_confirmado(banco, relogio)
        consulta_aprovada(mp, p)
        mp.post(url__regex=r"/v1/payments/sim-[0-9a-f]+/refunds$").mock(
            return_value=recusa()
        )

        resultado = entregar(
            consumidor_com(banco, real, relogio),
            CanalDeTeste(),
            comando(
                "EstornarPagamento",
                {"ordem_id": str(ordem_id), "motivo": "cancelamento"},
            ),
        )

        assert resultado == "processada"
        [rejeicao] = captura.eventos("mercadopago_request_rejected")
        assert rejeicao["operacao"] == "estornar"
        [recusado] = captura.eventos("refund_refused")
        assert recusado["pagamento_id"] == str(p["_id"])
        assert captura.vazamentos() == {}

    def test_falha_do_banco_ao_gravar_a_falha_do_estorno_nao_carrega_o_texto(
        self,
        banco: Banco,
        relogio: RelogioFixo,
        real: MercadoPagoGateway,
        mp: respx.MockRouter,
        captura: Captura,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """O 400 do estorno e tratado e a gravacao da falha quebra com um erro do banco
        que nao e transitorio: o consumidor escreve o traceback dele no log da DLQ, e a
        recusa nao pode estar na cadeia ``__context__`` desse erro."""
        ordem_id, p = pagamento_confirmado(banco, relogio)
        consulta_aprovada(mp, p)
        mp.post(url__regex=r"/v1/payments/sim-[0-9a-f]+/refunds$").mock(
            return_value=recusa()
        )

        def quebra(self: Pagamento, motivo: str) -> None:
            raise OperationFailure("falha de teste na gravacao")

        monkeypatch.setattr(Pagamento, "registrar_falha_de_estorno", quebra)
        consumidor, canal = consumidor_com(banco, real, relogio), CanalDeTeste()

        resultado = entregar(
            consumidor,
            canal,
            comando(
                "EstornarPagamento",
                {"ordem_id": str(ordem_id), "motivo": "cancelamento"},
            ),
        )

        assert resultado == "dlq"
        [rejeicao] = captura.eventos("mercadopago_request_rejected")
        assert rejeicao["operacao"] == "estornar"
        [dlq] = captura.eventos("command_dead_lettered")
        assert dlq["erro"] == "OperationFailure"
        assert "OperationFailure" in dlq["exception"]
        assert captura.vazamentos() == {}


class TestPrazos:
    def test_conciliacao_recusada_loga_so_o_evento_sem_campo_nem_traceback(
        self,
        banco: Banco,
        real: MercadoPagoGateway,
        mp: respx.MockRouter,
        captura: Captura,
    ) -> None:
        salvar_solicitado(banco)
        mp.get("/v1/payments/search").mock(return_value=recusa())

        ciclo_do_prazos(banco, real)

        [rejeicao] = captura.eventos("mercadopago_request_rejected")
        assert rejeicao["operacao"] == "buscar_pagamentos"
        [pausa] = captura.eventos("payment_reconciliation_refused")
        assert "exception" not in pausa
        assert captura.vazamentos() == {}

    def test_estorno_automatico_recusado_loga_so_o_contexto_do_pagamento(
        self,
        banco: Banco,
        real: MercadoPagoGateway,
        mp: respx.MockRouter,
        captura: Captura,
    ) -> None:
        p = salvar_solicitado(banco)
        aprovacao_de_valor_diferente(mp, p)
        mp.post("/v1/payments/777/refunds").mock(return_value=recusa())
        # A conferencia no provedor acha que o pagamento nao existe: a recusa vale.
        mp.get("/v1/payments/777").mock(return_value=recusa(404))

        ciclo_do_prazos(banco, real)

        [rejeicao] = captura.eventos("mercadopago_request_rejected")
        assert rejeicao["operacao"] == "estornar"
        [recusado] = captura.eventos("automatic_refund_refused")
        assert recusado["pagamento_id"] == str(p.id)
        assert captura.vazamentos() == {}

    def test_erro_inesperado_ao_conferir_o_estorno_automatico_nao_carrega_o_texto(
        self,
        banco: Banco,
        real: MercadoPagoGateway,
        mp: respx.MockRouter,
        captura: Captura,
    ) -> None:
        """Mesmo caso do consumidor, no estorno automatico: a conferencia no provedor
        depois da recusa quebra com um erro que o ciclo escreve com o traceback."""
        p = salvar_solicitado(banco)
        aprovacao_de_valor_diferente(mp, p)
        mp.post("/v1/payments/777/refunds").mock(return_value=recusa())
        mp.get("/v1/payments/777").mock(side_effect=RuntimeError("falha inesperada"))

        ciclo_do_prazos(banco, real)

        [falhou] = captura.eventos("payment_reconciliation_failed")
        assert "RuntimeError" in falhou["exception"]
        assert captura.vazamentos() == {}


class TestApi:
    def test_webhook_com_consulta_recusada_loga_so_o_codigo(
        self,
        banco: Banco,
        real: MercadoPagoGateway,
        mp: respx.MockRouter,
        captura: Captura,
    ) -> None:
        # criar_app configura o log de novo, para o stdout: a captura le dele tambem.
        app = criar_app(
            configuracao(), banco=banco, gateway=real, provedor=provedor_descartavel()
        )
        mp.get("/v1/payments/123").mock(return_value=recusa(403))

        with TestClient(app) as api:
            resposta = api.post(
                f"{WEBHOOK}?data.id=123&type=payment",
                json={"type": "payment", "data": {"id": "123"}},
                headers=assinatura("123"),
            )

        assert resposta.status_code == 409
        [tratada] = captura.eventos("domain_exception_handled")
        assert tratada["codigo"] == "GATEWAY_PAGAMENTO_RECUSOU"
        assert captura.vazamentos() == {}
