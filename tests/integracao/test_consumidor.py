"""Consumidor de ``billing.comandos``: origem, contrato, retry por nivel e DLQ.

Canal falso (anota publicacoes, acks e rejeicoes) e MongoDB real; o broker de
verdade entra em ``test_mensageria_rabbitmq.py``.
"""

from __future__ import annotations

import io
import json
import logging
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from pika.exceptions import AMQPConnectionError
from prometheus_client import REGISTRY
from pymongo.errors import (
    AutoReconnect,
    ExecutionTimeout,
    OperationFailure,
    WriteError,
)

from src.compartilhado.aplicacao.mensageria import Desfecho
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.mensageria.amqp import (
    CanalAmqp,
    MensagemRecusadaError,
)
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    ConsumidorDeComandos,
)
from src.compartilhado.infraestrutura.mensageria.telemetria import contexto_atual
from src.consumidor import rodar as rodar_consumidor
from src.orcamento.dominio.events import GeracaoDeOrcamentoFalhouEvent
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.pagamento.aplicacao.ports import (
    GatewayPagamentoIndisponivelError,
    GatewayPagamentoRecusouError,
)
from tests.factories import orcamento
from tests.integracao.apoio import (
    CanalDeTeste,
    RelogioFixo,
    comando,
    entregar,
    eventos_do_outbox,
    propriedades,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from pymongo.database import Database

    from src.compartilhado.infraestrutura.unit_of_work import UnidadeDaMensagem

    Banco = Database[dict[str, Any]]

_tracer = trace.get_tracer("teste")


class HandlerDeTeste:
    """Handler de ``CancelarOrcamento``: falha como mandado ou responde um evento."""

    def __init__(self) -> None:
        self.chamadas = 0
        self.erro: Exception | None = None

    def __call__(self, dados: Mapping[str, Any], uow: UnidadeDaMensagem) -> Desfecho:
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
        # Direto para a DLQ: sem copia de retry e sem ack silencioso.
        assert (canal.rejeitadas, canal.confirmadas, canal.publicadas) == ([1], [], [])
        [registro] = [
            r for r in caplog.records if r.getMessage() == "command_dead_lettered"
        ]
        assert registro.exc_info is not None
        assert registro.__dict__["erro"] == "KeyError"


class TestEntradaHostil:
    """Nenhuma excecao causada pela mensagem sai de ``tratar``, e nenhum campo
    dela vai para o log ou para o span antes da validacao (so cortado)."""

    def test_corpo_acima_do_teto_nao_chega_ao_parser(
        self,
        consumidor: ConsumidorDeComandos,
        handler: HandlerDeTeste,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        envelope = {**_cancelar(), "enchimento": "x" * 70_000}
        corpo = json.dumps(envelope).encode()
        canal = CanalDeTeste()

        with caplog.at_level(logging.ERROR):
            resultado = consumidor.tratar(canal, 1, propriedades(envelope), corpo)

        assert (resultado, canal.rejeitadas, handler.chamadas) == ("dlq", [1], 0)
        [registro] = [
            r for r in caplog.records if r.getMessage() == "command_dead_lettered"
        ]
        assert registro.__dict__["detalhe"] == (
            f"corpo de {len(corpo)} bytes, acima de 65536"
        )

    def test_json_aninhado_alem_da_pilha_do_parser_vai_para_a_dlq(
        self, consumidor: ConsumidorDeComandos
    ) -> None:
        # Numa thread de pilha curta o parser do json estoura a pilha
        # (RecursionError) antes do teto de tamanho do corpo.
        corpo = b"[" * 30_000 + b"]" * 30_000
        canal = CanalDeTeste()
        resultados: list[str] = []

        def tratar() -> None:
            resultados.append(
                consumidor.tratar(canal, 1, propriedades(_cancelar()), corpo)
            )

        thread = threading.Thread(target=tratar)
        anterior = threading.stack_size(256 * 1024)
        try:
            thread.start()
        finally:
            threading.stack_size(anterior)
        thread.join(timeout=30)

        assert (resultados, canal.rejeitadas) == (["dlq"], [1])

    def test_campo_fora_do_contrato_nao_vai_inteiro_para_o_log_nem_para_o_span(
        self,
        consumidor: ConsumidorDeComandos,
        caplog: pytest.LogCaptureFixture,
        spans: InMemorySpanExporter,
    ) -> None:
        envelope = _cancelar()
        hostil = "a." * 20_000
        props = propriedades(envelope)
        props.message_id = hostil
        corpo = json.dumps({**envelope, "id": hostil, "correlation_id": hostil})

        with caplog.at_level(logging.INFO):
            resultado = consumidor.tratar(CanalDeTeste(), 1, props, corpo.encode())

        assert resultado == "dlq"
        [registro] = [
            r for r in caplog.records if r.getMessage() == "command_dead_lettered"
        ]
        assert registro.__dict__["mensagem_id"] == hostil[:64]
        assert max(len(str(r.__dict__)) for r in caplog.records) < 2000
        assert spans.get_finished_spans() == ()

    @pytest.mark.parametrize(
        "etapa",
        ["_conferir_origem", "_tipo_conhecido"],
        ids=["origem", "tipo"],
    )
    def test_falha_inesperada_na_leitura_tambem_vai_para_a_dlq(
        self,
        consumidor: ConsumidorDeComandos,
        monkeypatch: pytest.MonkeyPatch,
        etapa: str,
    ) -> None:
        def defeito(*_args: object) -> None:
            raise TypeError("defeito")

        monkeypatch.setattr(ConsumidorDeComandos, etapa, defeito)
        canal = CanalDeTeste()

        assert entregar(consumidor, canal, _cancelar()) == "dlq"
        assert canal.rejeitadas == [1]

    def test_dlq_antes_da_validacao_loga_o_que_acha_a_mensagem(
        self, consumidor: ConsumidorDeComandos, caplog: pytest.LogCaptureFixture
    ) -> None:
        envelope = {**_cancelar(), "tipo": "TipoInventado"}
        antes = _consumidas("desconhecido", "dlq")

        with caplog.at_level(logging.ERROR):
            assert entregar(consumidor, CanalDeTeste(), envelope) == "dlq"

        [registro] = [
            r for r in caplog.records if r.getMessage() == "command_dead_lettered"
        ]
        assert (
            registro.__dict__["tipo"],
            registro.__dict__["mensagem_id"],
            registro.__dict__["correlation_id"],
            registro.__dict__["user_id"],
        ) == ("TipoInventado", envelope["id"], envelope["correlation_id"], "os")
        assert registro.__dict__["detalhe"] == (
            "tipo 'TipoInventado' sem handler neste consumidor"
        )
        # O rotulo da metrica fica fechado: o tipo inventado nao vira serie.
        assert _consumidas("desconhecido", "dlq") == antes + 1
        assert _consumidas("TipoInventado", "dlq") == 0


def _rotulado(rotulo: str) -> OperationFailure:
    # O driver le os rotulos de errorLabels na resposta do servidor.
    return OperationFailure("conflito", code=112, details={"errorLabels": [rotulo]})


class TestClassificacao:
    @pytest.mark.parametrize(
        ("erro", "resultado"),
        [
            (AutoReconnect("primario caiu"), "retry"),
            (ExecutionTimeout("operacao longa"), "retry"),
            (_rotulado("TransientTransactionError"), "retry"),
            (_rotulado("RetryableWriteError"), "retry"),
            (GatewayPagamentoIndisponivelError(), "retry"),
            (ConnectionResetError("rede"), "retry"),
            (TimeoutError("rede"), "retry"),
            (WriteError("documento recusado pelo validador", code=121), "dlq"),
            (OperationFailure("sem permissao", code=13), "dlq"),
            (ValueError("defeito"), "dlq"),
        ],
        ids=[
            "auto-reconnect",
            "execution-timeout",
            "transacao-transitoria",
            "escrita-repetivel",
            "provedor-fora",
            "conexao",
            "timeout",
            "validador-do-banco",
            "permissao-no-banco",
            "defeito",
        ],
    )
    def test_so_o_que_passa_sozinho_volta_pela_fila_de_retry(
        self,
        consumidor: ConsumidorDeComandos,
        handler: HandlerDeTeste,
        erro: Exception,
        resultado: str,
    ) -> None:
        handler.erro = erro
        assert entregar(consumidor, CanalDeTeste(), _cancelar()) == resultado

    def test_dlq_por_regra_de_negocio_loga_o_codigo_e_nao_o_texto(
        self,
        consumidor: ConsumidorDeComandos,
        handler: HandlerDeTeste,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        handler.erro = GatewayPagamentoRecusouError("texto livre do provedor")
        with caplog.at_level(logging.ERROR):
            assert entregar(consumidor, CanalDeTeste(), _cancelar()) == "dlq"
        [registro] = [
            r for r in caplog.records if r.getMessage() == "command_dead_lettered"
        ]
        assert registro.__dict__["codigo"] == "GATEWAY_PAGAMENTO_RECUSOU"
        assert "texto livre" not in str(registro.__dict__)


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
        self,
        consumidor: ConsumidorDeComandos,
        handler: HandlerDeTeste,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        handler.erro = AutoReconnect("banco fora")
        canal = CanalDeTeste(erro_ao_publicar=MensagemRecusadaError("NackError"))
        with caplog.at_level(logging.ERROR):
            assert entregar(consumidor, canal, _cancelar()) == "dlq"
        assert (canal.confirmadas, canal.rejeitadas) == ([], [1])
        [registro] = [
            r for r in caplog.records if r.getMessage() == "command_dead_lettered"
        ]
        assert (registro.__dict__["erro"], registro.__dict__["detalhe"]) == (
            "MensagemRecusadaError",
            "NackError",
        )
        assert registro.exc_info is None


class TestTransacaoDaMensagem:
    """O handler grava sem comitar: o consumidor comita efeito, outbox e
    ``mensagens_processadas`` juntos, ou nada (RFC-004, secao 5.4)."""

    @pytest.mark.parametrize(
        ("erro", "resultado"),
        [
            (AutoReconnect("banco caiu depois do efeito"), "retry"),
            (KeyError("bug"), "dlq"),
        ],
        ids=["transitorio-vai-para-o-retry", "defeito-vai-para-a-dlq"],
    )
    def test_falha_depois_do_efeito_nao_grava_nada_e_segue_a_escada(
        self,
        banco: Banco,
        relogio: RelogioFixo,
        erro: Exception,
        resultado: str,
    ) -> None:
        def efeito_e_falha(
            _dados: Mapping[str, Any], uow: UnidadeDaMensagem
        ) -> Desfecho:
            uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(orcamento()))
            raise erro

        consumidor = ConsumidorDeComandos(
            banco,
            {"CancelarOrcamento": efeito_e_falha},
            fila="billing.comandos",
            usuario="billing",
            relogio=relogio,
        )
        canal = CanalDeTeste()

        assert entregar(consumidor, canal, _cancelar()) == resultado

        assert banco["orcamentos"].count_documents({}) == 0
        assert eventos_do_outbox(banco) == []
        assert banco["mensagens_processadas"].count_documents({}) == 0
        filas = [routing_key for _, routing_key, *_ in canal.publicadas]
        assert filas == (["billing.comandos.retry.1s"] if resultado == "retry" else [])

    def test_efeito_outbox_e_mensagem_comitam_juntos(
        self, banco: Banco, relogio: RelogioFixo
    ) -> None:
        def efeito(_dados: Mapping[str, Any], uow: UnidadeDaMensagem) -> Desfecho:
            uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(orcamento()))
            return Desfecho.PROCESSADA

        consumidor = ConsumidorDeComandos(
            banco,
            {"CancelarOrcamento": efeito},
            fila="billing.comandos",
            usuario="billing",
            relogio=relogio,
        )
        envelope = _cancelar()

        assert entregar(consumidor, CanalDeTeste(), envelope) == "processada"

        assert banco["orcamentos"].count_documents({}) == 1
        assert [e["causation_id"] for e in eventos_do_outbox(banco)] == [envelope["id"]]
        assert banco["mensagens_processadas"].count_documents({}) == 1


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
    def test_logs_do_handler_levam_o_trace_do_span_do_consumidor(
        self,
        banco: Banco,
        handler: HandlerDeTeste,
        relogio: RelogioFixo,
        spans: InMemorySpanExporter,
    ) -> None:
        saida = io.StringIO()
        configurar_logging(saida)
        registros = logging.getLogger("teste.handler")

        def com_log(dados: Mapping[str, Any], uow: UnidadeDaMensagem) -> Desfecho:
            registros.info("handler_called")
            return handler(dados, uow)

        consumidor = ConsumidorDeComandos(
            banco,
            {"CancelarOrcamento": com_log},
            fila="billing.comandos",
            usuario="billing",
            relogio=relogio,
        )
        envelope = _cancelar()
        try:
            registros.info("fora_do_span")
            entregar(consumidor, CanalDeTeste(), envelope)
        finally:
            logging.getLogger().handlers.clear()

        linhas = [json.loads(linha) for linha in saida.getvalue().splitlines()]
        [do_handler] = [linha for linha in linhas if linha["event"] == "handler_called"]
        [consumo] = [
            s for s in spans.get_finished_spans() if s.kind is SpanKind.CONSUMER
        ]
        assert do_handler["trace_id"] == f"{consumo.context.trace_id:032x}"
        assert do_handler["span_id"] == f"{consumo.context.span_id:016x}"
        assert do_handler["correlation_id"] == envelope["correlation_id"]
        [fora] = [linha for linha in linhas if linha["event"] == "fora_do_span"]
        assert "trace_id" not in fora

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


class CanalDoBrokerFalso:
    """``BlockingChannel`` de mentira: entrega uma lista e anota o cancelamento."""

    def __init__(self, entregas: list[tuple[Any, Any, Any]]) -> None:
        self.entregas = entregas
        self.prefetch: int | None = None
        self.cancelado = False

    def basic_qos(self, prefetch_count: int) -> None:
        self.prefetch = prefetch_count

    def consume(
        self, fila: str, inactivity_timeout: float
    ) -> Iterator[tuple[Any, Any, Any]]:
        assert (fila, inactivity_timeout) == ("billing.comandos", 0.01)
        yield from self.entregas

    def cancel(self) -> None:
        self.cancelado = True


class CanalAmqpFalso(CanalAmqp):
    """Falha ao abrir N vezes; cada conexao entrega a lista da vez."""

    def __init__(
        self, falhas_ao_abrir: int, conexoes: list[CanalDoBrokerFalso]
    ) -> None:
        self.falhas_ao_abrir = falhas_ao_abrir
        self.aberturas = 0
        self.conexoes = conexoes
        self.atual: CanalDoBrokerFalso | None = None
        self.respostas = CanalDeTeste()

    def abrir(self) -> None:
        self.aberturas += 1
        if self.aberturas <= self.falhas_ao_abrir:
            raise AMQPConnectionError("broker fora")
        self.atual = self.conexoes.pop(0)

    @property
    def canal(self) -> CanalDoBrokerFalso | None:
        return self.atual

    def confirmar(self, entrega: int) -> None:
        self.respostas.confirmar(entrega)

    def rejeitar(self, entrega: int) -> None:
        self.respostas.rejeitar(entrega)

    def aguardar(self, segundos: float) -> None:
        return None

    def fechar(self) -> None:
        self.atual = None


class TestLacoDoProcesso:
    def test_reconecta_consome_e_para_devolvendo_as_pre_buscadas(
        self, consumidor: ConsumidorDeComandos, tmp_path: Path
    ) -> None:
        envelope = _cancelar()
        corpo = json.dumps(envelope).encode()
        metodo = SimpleNamespace(delivery_tag=1)
        parar = threading.Event()

        class Ultima(CanalDoBrokerFalso):
            def consume(
                self, fila: str, inactivity_timeout: float
            ) -> Iterator[tuple[Any, Any, Any]]:
                yield (metodo, propriedades(envelope), corpo)
                parar.set()
                yield (None, None, None)

        # 1a conexao: o broker encerra o consumo (o gerador acaba); 2a: entrega.
        primeira, segunda = CanalDoBrokerFalso([(None, None, None)]), Ultima([])
        canal = CanalAmqpFalso(falhas_ao_abrir=2, conexoes=[primeira, segunda])
        heartbeat = tmp_path / "consumidor"

        rodar_consumidor(
            consumidor,
            canal,
            parar=parar,
            heartbeat=heartbeat,
            inatividade=0.01,
            espera_maxima=0.01,
        )

        assert canal.aberturas == 4
        assert (primeira.prefetch, segunda.prefetch) == (1, 1)
        assert (primeira.cancelado, segunda.cancelado) == (False, True)
        assert canal.respostas.confirmadas == [1]
        assert heartbeat.read_text() == "conectando"
