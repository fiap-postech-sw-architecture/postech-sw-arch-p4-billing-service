"""OpenTelemetry da mensageria: contexto W3C na outbox e nos headers AMQP (ADR-043).

Relay e consumidor instalam o ``TracerProvider`` do SDK sempre, para o contexto
seguir de mensagem em mensagem; a exportacao OTLP/gRPC so liga com
``OTEL_ENABLED=true`` (endpoint em ``OTEL_EXPORTER_OTLP_ENDPOINT``), os mesmos
nomes do contrato de configuracao da plataforma. A API nao instala o SDK: sem
span corrente, o evento espontaneo segue o contexto guardado no registro que
esperava por ele (``aberto_por``, na unidade de trabalho).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opentelemetry.context import Context

# So traceparent/tracestate: o que o envelope do contrato carrega (sem baggage).
_PROPAGADOR: Final = TraceContextTextMapPropagator()
_VERDADEIROS: Final = frozenset({"true", "1"})
_TAMANHO_MAXIMO_TRACESTATE: Final = 512
# Padroes do compose da plataforma (Jaeger por OTLP/gRPC).
_ENDPOINT_PADRAO: Final = "http://jaeger:4317"
_SERVICO_PADRAO: Final = "billing-service"

# ProxyTracer: vale o provider instalado depois (processo ou testes).
tracer = trace.get_tracer("pytstop.billing.mensageria")


def contexto_atual() -> dict[str, str]:
    """``traceparent``/``tracestate`` do span corrente; vazio fora de span."""
    portador: dict[str, str] = {}
    _PROPAGADOR.inject(portador)
    return portador


def contexto_de(portador: Mapping[str, object]) -> Context:
    """Contexto W3C lido de headers AMQP ou de um documento da outbox.

    ``tracestate`` acima do limite do W3C Trace Context (512 caracteres) e
    descartado: o header vem de fora e seria regravado em cada outbox e copia
    de retry; o ``traceparent`` segue valendo.
    """
    textos = {
        chave: valor for chave, valor in portador.items() if isinstance(valor, str)
    }
    if len(textos.get("tracestate", "")) > _TAMANHO_MAXIMO_TRACESTATE:
        del textos["tracestate"]
    return _PROPAGADOR.extract(textos)


def criar_provedor(ambiente: Mapping[str, str], *, processo: str) -> TracerProvider:
    """``TracerProvider`` do processo; com ``OTEL_ENABLED``, exporta por OTLP."""
    provedor = TracerProvider(
        resource=Resource.create(
            {
                "service.name": ambiente.get("OTEL_SERVICE_NAME") or _SERVICO_PADRAO,
                "service.version": ambiente.get("PYTSTOP_GIT_SHA", "unknown")[:12],
                "pytstop.processo": processo,
            }
        )
    )
    if ambiente.get("OTEL_ENABLED", "").strip().lower() in _VERDADEIROS:
        endpoint = ambiente.get("OTEL_EXPORTER_OTLP_ENDPOINT") or _ENDPOINT_PADRAO
        provedor.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=endpoint, insecure=endpoint.startswith("http://")
                )
            )
        )
    return provedor


def configurar_telemetria(processo: str) -> TracerProvider:
    """Instala o provider do processo; ``shutdown`` no fim descarrega os spans."""
    provedor = criar_provedor(os.environ, processo=processo)
    trace.set_tracer_provider(provedor)
    return provedor
