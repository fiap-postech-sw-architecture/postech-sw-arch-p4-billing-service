"""Contexto W3C da mensageria e provider do OpenTelemetry por processo."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc import trace_exporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind
from starlette.testclient import TestClient

from src.compartilhado.infraestrutura.mensageria import telemetria
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    instrumentar_clientes,
)
from src.main import criar_app
from tests.integracao.apoio import configuracao

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
# Mesmo trace, com a flag de amostragem desligada.
TRACEPARENT_NAO_AMOSTRADO = TRACEPARENT.removesuffix("01") + "00"


def test_fora_de_span_nao_ha_contexto() -> None:
    assert telemetria.contexto_atual() == {}


def test_contexto_do_span_corrente_e_lido_de_volta(
    spans: InMemorySpanExporter,
) -> None:
    pai = telemetria.contexto_de({"traceparent": TRACEPARENT, "x-tentativa": 2})
    with telemetria.tracer.start_as_current_span("filho", context=pai) as span:
        portador = telemetria.contexto_atual()
    assert portador["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    [terminado] = spans.get_finished_spans()
    assert terminado.parent is not None
    assert terminado.parent.span_id == 0x00F067AA0BA902B7
    assert span.get_span_context().trace_id == 0x4BF92F3577B34DA6A3CE929D0E0E4736


def test_exportacao_otlp_so_com_otel_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    exportadores: list[dict[str, Any]] = []

    def exportador(**opcoes: Any) -> InMemorySpanExporter:
        exportadores.append(opcoes)
        return InMemorySpanExporter()

    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", exportador)
    telemetria.criar_provedor({}, processo="relay").shutdown()
    assert exportadores == []
    telemetria.criar_provedor(
        {"OTEL_ENABLED": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://jaeger:4317"},
        processo="relay",
    ).shutdown()
    telemetria.criar_provedor(
        {"OTEL_ENABLED": "1", "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.teste"},
        processo="relay",
    ).shutdown()
    assert exportadores == [
        {"endpoint": "http://jaeger:4317", "insecure": True},
        {"endpoint": "https://otlp.teste", "insecure": False},
    ]


def test_recurso_identifica_servico_versao_e_processo() -> None:
    provedor = telemetria.criar_provedor(
        {"OTEL_SERVICE_NAME": "", "PYTSTOP_GIT_SHA": "0123456789abcdef"},
        processo="consumidor",
    )
    atributos = dict(provedor.resource.attributes)
    provedor.shutdown()
    assert atributos["service.name"] == "billing-service"
    assert atributos["service.version"] == "0123456789ab"
    assert atributos["pytstop.processo"] == "consumidor"


def test_configurar_instala_o_provider_global_e_a_instrumentacao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    passos: list[object] = []
    monkeypatch.setattr(trace, "set_tracer_provider", passos.append)
    monkeypatch.setattr(telemetria, "instrumentar_clientes", passos.append)
    provedor = telemetria.configurar_telemetria("relay")
    # Na ordem: o PyMongo e o httpx usam o provider que acabou de ser instalado.
    assert passos == [provedor, provedor]
    provedor.shutdown()


def test_instrumenta_o_mongodb_sem_capturar_o_comando_e_o_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``capture_statement`` poria o documento (com link, checkout_url e valor)
    no atributo do span."""
    chamadas: list[tuple[str, dict[str, Any]]] = []

    class Instrumentador:
        def __init__(self, nome: str) -> None:
            self.nome = nome

        def instrument(self, **opcoes: Any) -> None:
            chamadas.append((self.nome, opcoes))

    monkeypatch.setattr(telemetria, "PymongoInstrumentor", lambda: Instrumentador("db"))
    monkeypatch.setattr(
        telemetria, "HTTPXClientInstrumentor", lambda: Instrumentador("http")
    )
    provedor = telemetria.criar_provedor({}, processo="api")

    instrumentar_clientes(provedor)

    assert chamadas == [
        ("db", {"tracer_provider": provedor, "capture_statement": False}),
        ("http", {"tracer_provider": provedor}),
    ]
    provedor.shutdown()


class TestAmostrador:
    """O amostrador do processo: span de cliente sem pai nao vira trace."""

    def test_provider_usa_o_amostrador_de_raiz_sob_o_pai(self) -> None:
        provedor = telemetria.criar_provedor({}, processo="teste")
        descricao = provedor.sampler.get_description()
        assert descricao.startswith("ParentBased{root:AmostradorDeRaiz{")
        provedor.shutdown()

    @pytest.fixture
    def tracer(self) -> Any:
        return telemetria.criar_provedor({}, processo="teste").get_tracer("teste")

    def test_cliente_sem_pai_e_descartado(self, tracer: Any) -> None:
        with tracer.start_as_current_span("find", kind=SpanKind.CLIENT) as span:
            assert not span.is_recording()
            assert not span.get_span_context().trace_flags.sampled

    @pytest.mark.parametrize(
        "tipo",
        [SpanKind.SERVER, SpanKind.PRODUCER, SpanKind.CONSUMER, SpanKind.INTERNAL],
    )
    def test_demais_raizes_sao_gravadas(self, tracer: Any, tipo: SpanKind) -> None:
        with tracer.start_as_current_span("trabalho", kind=tipo) as span:
            assert span.is_recording()
            assert span.get_span_context().trace_flags.sampled

    def test_cliente_dentro_de_um_trace_segue_o_pai(self, tracer: Any) -> None:
        with (
            tracer.start_as_current_span("GET /x", kind=SpanKind.SERVER),
            tracer.start_as_current_span("find", kind=SpanKind.CLIENT) as filho,
        ):
            assert filho.is_recording()

    def test_filho_de_um_cliente_descartado_tambem_e_descartado(
        self, tracer: Any
    ) -> None:
        with (
            tracer.start_as_current_span("find", kind=SpanKind.CLIENT),
            tracer.start_as_current_span("filho") as filho,
        ):
            assert not filho.is_recording()

    def test_pai_remoto_amostrado_manda_ate_no_cliente(self, tracer: Any) -> None:
        pai = telemetria.contexto_de({"traceparent": TRACEPARENT})
        with tracer.start_as_current_span(
            "find", context=pai, kind=SpanKind.CLIENT
        ) as span:
            assert span.is_recording()

    def test_pai_remoto_nao_amostrado_descarta_ate_o_server(self, tracer: Any) -> None:
        pai = telemetria.contexto_de({"traceparent": TRACEPARENT_NAO_AMOSTRADO})
        with tracer.start_as_current_span(
            "GET /x", context=pai, kind=SpanKind.SERVER
        ) as span:
            assert not span.is_recording()


def test_tracestate_acima_do_limite_w3c_e_descartado(
    spans: InMemorySpanExporter,
) -> None:
    # 32 membros de 16 caracteres: dentro do limite de membros, acima de 512.
    grande = ",".join(f"k{i:02d}=v{'x' * 11}" for i in range(32))
    assert len(grande) > 512
    curto = "pytstop=abc"
    for estado, esperado in ((grande, ""), (curto, curto)):
        pai = telemetria.contexto_de({"traceparent": TRACEPARENT, "tracestate": estado})
        with telemetria.tracer.start_as_current_span("filho", context=pai):
            portador = telemetria.contexto_atual()
        assert portador.get("tracestate", "") == esperado
        assert portador["traceparent"].startswith(
            "00-4bf92f3577b34da6a3ce929d0e0e4736-"
        )


def test_tracestate_de_exatamente_512_caracteres_e_mantido(
    spans: InMemorySpanExporter,
) -> None:
    # 16 membros de 31 ou 32 caracteres: exatamente o limite do W3C.
    exato = ",".join(f"k{i:02d}={'v' * (27 if i < 15 else 28)}" for i in range(16))
    assert len(exato) == 512
    pai = telemetria.contexto_de({"traceparent": TRACEPARENT, "tracestate": exato})
    with telemetria.tracer.start_as_current_span("filho", context=pai):
        assert telemetria.contexto_atual()["tracestate"] == exato


def test_api_nao_deixa_o_fastapi_montar_o_export_otlp_na_partida(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Com o endpoint OTLP no ambiente (como no cluster), o FastAPI tentaria
    somar exportadores http/protobuf na partida e, sem o extra
    ``fastapi[opentelemetry]``, avisaria que a configuracao falhou. O
    ``auto_configure`` desligado mantem o log de boot limpo."""
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger.pytstop-plataforma:4317"
    )
    avisos: list[str] = []

    class Coletor(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            avisos.append(record.getMessage())

    logger = logging.getLogger("fastapi")
    coletor = Coletor(level=logging.WARNING)
    logger.addHandler(coletor)
    try:
        # Banco injetado: o lifespan nao abre conexao.
        with TestClient(criar_app(configuracao(), banco=MagicMock())):
            pass
    finally:
        logger.removeHandler(coletor)

    assert avisos == []
