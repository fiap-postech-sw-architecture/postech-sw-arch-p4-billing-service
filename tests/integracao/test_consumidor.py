"""Consumidor de ``billing.comandos``: origem, contrato, retry por nivel e DLQ.

Canal falso (anota publicacoes, acks e rejeicoes) e MongoDB real; o broker de
verdade entra em ``test_mensageria_rabbitmq.py``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from prometheus_client import REGISTRY
from pymongo.errors import AutoReconnect

from src.compartilhado.infraestrutura.mensageria.amqp import MensagemRecusadaError
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    ConsumidorDeComandos,
    Desfecho,
)
from src.compartilhado.infraestrutura.mensageria.telemetria import contexto_atual
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.orcamento.dominio.events import GeracaoDeOrcamentoFalhouEvent
from tests.integracao.apoio import (
    CanalDeTeste,
    RelogioFixo,
    comando,
    entregar,
    eventos_do_outbox,
    propriedades,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from pymongo.database import Database

    Banco = Database[dict[str, Any]]

_tracer = trace.get_tracer("teste")


class HandlerDeTeste:
    """Handler de ``CancelarOrcamento``: falha como mandado ou responde um evento."""

    def __init__(self) -> None:
        self.chamadas = 0
        self.erro: Exception | None = None

    def __call__(self, dados: Mapping[str, Any], uow: MongoUnitOfWork) -> Desfecho:
        self.chamadas += 1
        if self.erro is not None:
            raise self.erro
        ordem_id = UUID(dados["ordem_id"])
        uow.executar(
            lambda: uow.registrar_evento(
                GeracaoDeOrcamentoFalhouEvent(
                    ordem_id=ordem_id, motivo="teste", codigos_invalidos=()
                )
            )
        )
        return Desfecho.PROCESSADA


@pytest.fixture
def handler() -> HandlerDeTeste:
    return HandlerDeTeste()


@pytest.fixture
def consumidor(
    banco: Banco, handler: HandlerDeTeste, relogio: RelogioFixo
) -> ConsumidorDeComandos:
    return ConsumidorDeComandos(
        banco,
        {"CancelarOrcamento": handler},
        fila="billing.comandos",
        usuario="billing",
        relogio=relogio,
    )


def _cancelar(ordem_id: UUID | None = None) -> dict[str, Any]:
    dados = {"ordem_id": str(ordem_id or uuid4()), "motivo": "cancelamento"}
    return comando("CancelarOrcamento", dados)


def _consumidas(tipo: str, resultado: str) -> float:
    valor = REGISTRY.get_sample_value(
        "pytstop_mensagens_consumidas_total", {"tipo": tipo, "resultado": resultado}
    )
    return valor or 0.0


class TestOrigem:
    def test_comando_do_os_e_processado(
        self, consumidor: ConsumidorDeComandos, handler: HandlerDeTeste
    ) -> None:
        canal = CanalDeTeste()
        antes = _consumidas("CancelarOrcamento", "processada")

        assert entregar(consumidor, canal, _cancelar()) == "processada"

        assert (canal.confirmadas, canal.rejeitadas, handler.chamadas) == ([1], [], 1)
        assert _consumidas("CancelarOrcamento", "processada") == antes + 1

    @pytest.mark.parametrize(
        ("user_id", "cabecalhos"),
        [
            ("execucao", {}),
            (None, {}),
            ("billing", {}),
            ("execucao", {"x-tentativa": 2}),
        ],
        ids=["outro-servico", "sem-user-id", "proprio-sem-retry", "outro-no-retry"],
    )
    def test_user_id_fora_da_regra_vai_direto_para_a_dlq(
        self,
        consumidor: ConsumidorDeComandos,
        handler: HandlerDeTeste,
        user_id: str | None,
        cabecalhos: dict[str, Any],
    ) -> None:
        canal = CanalDeTeste()
        resultado = entregar(
            consumidor, canal, _cancelar(), user_id=user_id, cabecalhos=cabecalhos
        )
        assert (resultado, canal.rejeitadas, handler.chamadas) == ("dlq", [1], 0)

    def test_copia_de_retry_com_o_proprio_usuario_e_aceita(
        self, consumidor: ConsumidorDeComandos, handler: HandlerDeTeste
    ) -> None:
        canal = CanalDeTeste()
        resultado = entregar(
            consumidor,
            canal,
            _cancelar(),
            user_id="billing",
            cabecalhos={"x-tentativa": 3},
        )
        assert (resultado, canal.confirmadas, handler.chamadas) == (
            "processada",
            [1],
            1,
        )


class TestErrosPermanentes:
    def test_corpo_que_nao_e_json(
        self, consumidor: ConsumidorDeComandos, handler: HandlerDeTeste
    ) -> None:
        canal = CanalDeTeste()
        antes = _consumidas("desconhecido", "dlq")
        envelope = _cancelar()
        resultado = consumidor.tratar(canal, 7, propriedades(envelope), b"{nao json")
        assert (resultado, canal.rejeitadas, handler.chamadas) == ("dlq", [7], 0)
        assert _consumidas("desconhecido", "dlq") == antes + 1

    def test_corpo_que_nao_e_envelope(self, consumidor: ConsumidorDeComandos) -> None:
        canal = CanalDeTeste()
        resultado = consumidor.tratar(canal, 1, propriedades(_cancelar()), b"[1, 2]")
        assert (resultado, canal.rejeitadas) == ("dlq", [1])

    def test_tipo_sem_handler(self, consumidor: ConsumidorDeComandos) -> None:
        envelope = _cancelar()
        envelope["tipo"] = "ReservarPecas"
        canal = CanalDeTeste()
        assert entregar(consumidor, canal, envelope) == "dlq"

    @pytest.mark.parametrize(
        "mudanca",
        [
            {"versao": 2},
            {"origem": "outro-servico"},
            {"dados": {"ordem_id": "nao-e-uuid", "motivo": "cancelamento"}},
            {"dados": {"ordem_id": str(uuid4()), "motivo": "texto livre"}},
        ],
        ids=[
            "versao-desconhecida",
            "origem-invalida",
            "uuid-invalido",
            "motivo-fora-da-lista",
        ],
    )
    def test_fora_do_contrato_nao_chega_ao_handler(
        self,
        consumidor: ConsumidorDeComandos,
        handler: HandlerDeTeste,
        mudanca: dict[str, Any],
    ) -> None:
        canal = CanalDeTeste()
        envelope = {**_cancelar(), **mudanca}
        assert entregar(consumidor, canal, envelope) == "dlq"
        assert handler.chamadas == 0

    @pytest.mark.parametrize(
        "valor", ["2", -1, True], ids=["texto", "negativo", "bool"]
    )
    def test_x_tentativa_invalido(
        self, consumidor: ConsumidorDeComandos, valor: object
    ) -> None:
        canal = CanalDeTeste()
        resultado = entregar(
            consumidor, canal, _cancelar(), cabecalhos={"x-tentativa": valor}
        )
        assert resultado == "dlq"

    def test_erro_inesperado_vai_para_a_dlq_com_o_traceback_no_log(
        self,
        consumidor: ConsumidorDeComandos,
        handler: HandlerDeTeste,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        handler.erro = KeyError("bug")
        canal = CanalDeTeste()
        with caplog.at_level(logging.ERROR):
            assert entregar(consumidor, canal, _cancelar()) == "dlq"
        [registro] = [
            r for r in caplog.records if r.getMessage() == "command_dead_lettered"
        ]
        assert registro.exc_info is not None
        assert registro.__dict__["erro"] == "KeyError"


class TestRetry:
    @pytest.mark.parametrize(
        ("tentativa", "fila"),
        [
            (0, "billing.comandos.retry.1s"),
            (1, "billing.comandos.retry.5s"),
            (2, "billing.comandos.retry.15s"),
            (3, "billing.comandos.retry.60s"),
            (4, "billing.comandos.retry.300s"),
        ],
    )
    def test_erro_transitorio_vai_para_a_fila_do_nivel_seguinte(
        self,
        consumidor: ConsumidorDeComandos,
        handler: HandlerDeTeste,
        tentativa: int,
        fila: str,
        spans: InMemorySpanExporter,
    ) -> None:
        handler.erro = AutoReconnect("banco fora")
        canal = CanalDeTeste()
        envelope = _cancelar()
        cabecalhos = {"x-tentativa": tentativa} if tentativa else {}
        usuario = "billing" if tentativa else "os"

        resultado = entregar(
            consumidor, canal, envelope, user_id=usuario, cabecalhos=cabecalhos
        )

        assert (resultado, canal.confirmadas, canal.rejeitadas) == ("retry", [1], [])
        [(exchange, routing_key, corpo, copia)] = canal.publicadas
        assert (exchange, routing_key) == ("pytstop.retry", fila)
        assert json.loads(corpo) == envelope
        assert copia.headers["x-tentativa"] == tentativa + 1
        # Sem expiration: o atraso e o TTL da propria fila de retry.
        assert copia.expiration is None
        assert copia.user_id == "billing"
        assert copia.message_id == envelope["id"]
        assert copia.delivery_mode == 2
        [consumo] = [
            s for s in spans.get_finished_spans() if s.kind is SpanKind.CONSUMER
        ]
        assert (
            copia.headers["traceparent"].split("-")[2]
            == f"{consumo.context.span_id:016x}"
        )

    def test_sexta_falha_vai_para_a_dlq(
        self, consumidor: ConsumidorDeComandos, handler: HandlerDeTeste
    ) -> None:
        handler.erro = AutoReconnect("banco fora")
        canal = CanalDeTeste()
        resultado = entregar(
            consumidor,
            canal,
            _cancelar(),
            user_id="billing",
            cabecalhos={"x-tentativa": 5},
        )
        assert (resultado, canal.publicadas, canal.rejeitadas) == ("dlq", [], [1])

    def test_copia_recusada_pelo_broker_leva_a_original_para_a_dlq(
        self, consumidor: ConsumidorDeComandos, handler: HandlerDeTeste
    ) -> None:
        handler.erro = AutoReconnect("banco fora")
        canal = CanalDeTeste(erro_ao_publicar=MensagemRecusadaError("NackError"))
        assert entregar(consumidor, canal, _cancelar()) == "dlq"
        assert (canal.confirmadas, canal.rejeitadas) == ([], [1])


class TestIdempotencia:
    def test_mesmo_id_roda_o_handler_e_conta_como_duplicada(
        self, banco: Banco, consumidor: ConsumidorDeComandos, handler: HandlerDeTeste
    ) -> None:
        envelope = _cancelar()
        canal = CanalDeTeste()
        antes = _consumidas("CancelarOrcamento", "duplicada")

        entregar(consumidor, canal, envelope)
        assert entregar(consumidor, canal, envelope) == "duplicada"

        # O handler decide a republicacao; o registro da mensagem e um so.
        assert handler.chamadas == 2
        assert banco["mensagens_processadas"].count_documents({}) == 1
        assert _consumidas("CancelarOrcamento", "duplicada") == antes + 1


class TestRastreamento:
    def test_span_do_consumidor_e_filho_da_publicacao_e_vai_para_a_outbox(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        spans: InMemorySpanExporter,
    ) -> None:
        envelope = _cancelar()
        with _tracer.start_as_current_span("publish CancelarOrcamento") as publicacao:
            cabecalhos = contexto_atual()

        entregar(consumidor, CanalDeTeste(), envelope, cabecalhos=cabecalhos)

        [consumo] = [
            s for s in spans.get_finished_spans() if s.kind is SpanKind.CONSUMER
        ]
        assert consumo.name == "process CancelarOrcamento"
        assert consumo.parent is not None
        assert consumo.parent.span_id == publicacao.get_span_context().span_id
        assert consumo.attributes is not None
        assert consumo.attributes["messaging.message.id"] == envelope["id"]
        [linha] = banco["outbox"].find()
        assert linha["traceparent"].split("-")[2] == f"{consumo.context.span_id:016x}"
        assert eventos_do_outbox(banco)[0]["causation_id"] == envelope["id"]
