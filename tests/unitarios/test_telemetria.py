"""Contexto W3C da mensageria e provider do OpenTelemetry por processo."""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc import trace_exporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from src.compartilhado.infraestrutura.mensageria import telemetria

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


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


def test_configurar_instala_o_provider_global(monkeypatch: pytest.MonkeyPatch) -> None:
    instalados: list[object] = []
    monkeypatch.setattr(trace, "set_tracer_provider", instalados.append)
    provedor = telemetria.configurar_telemetria("relay")
    assert instalados == [provedor]
    provedor.shutdown()


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
