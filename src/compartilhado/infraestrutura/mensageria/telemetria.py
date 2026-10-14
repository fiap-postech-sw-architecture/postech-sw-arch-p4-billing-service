"""OpenTelemetry do Billing: contexto W3C na outbox e nos headers AMQP (ADR-043).

API, relay, consumidor e ``prazos`` instalam o ``TracerProvider`` do SDK sempre,
para o contexto seguir de mensagem em mensagem e o ``trace_id`` chegar ao log; a
exportacao OTLP/gRPC so liga com ``OTEL_ENABLED=true`` (endpoint em
``OTEL_EXPORTER_OTLP_ENDPOINT``), os mesmos nomes do contrato de configuracao da
plataforma. O evento espontaneo segue o contexto guardado no registro que
esperava por ele (``aberto_por``, na unidade de trabalho), com span link para o
span de quem retomou o passo: a requisicao da API ou o ciclo do ``prazos``.

O amostrador descarta o span de cliente (consulta ao MongoDB, chamada HTTP) que
nasce sem pai: e o poll do relay, a consulta do coletor de metricas e o ciclo
ocioso do ``prazos``, que nao devem encher o Jaeger. Span de cliente dentro de
um trace (requisicao, mensagem, ciclo com trabalho) segue o pai.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    Sampler,
    SamplingResult,
)
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from opentelemetry.context import Context
    from opentelemetry.trace import Link, TraceState
    from opentelemetry.util.types import Attributes

# So traceparent/tracestate: o que o envelope do contrato carrega (sem baggage).
_PROPAGADOR: Final = TraceContextTextMapPropagator()
_VERDADEIROS: Final = frozenset({"true", "1"})
_TAMANHO_MAXIMO_TRACESTATE: Final = 512
# Padroes do compose da plataforma (Jaeger por OTLP/gRPC).
_ENDPOINT_PADRAO: Final = "http://jaeger:4317"
_SERVICO_PADRAO: Final = "billing-service"

# ProxyTracer: vale o provider instalado depois (processo ou testes).
tracer = trace.get_tracer("pytstop.billing.mensageria")


class AmostradorDeRaiz(Sampler):
    """Amostrador do span que abre um trace: aceita tudo, menos span de cliente.

    Entra como ``root`` de um ``ParentBased``: com pai, quem decide e o pai.
    """

    def should_sample(  # noqa: PLR0913 - assinatura do Sampler do SDK
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        decisor = ALWAYS_OFF if kind is SpanKind.CLIENT else ALWAYS_ON
        return decisor.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        return "AmostradorDeRaiz{descarta span de cliente sem pai}"


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


def links_de(portador: Mapping[str, object]) -> list[trace.Link]:
    """Span link para o contexto W3C do portador, quando ele e valido (ADR-043:
    o evento de um passo retomado aponta para o trace de quem o retomou)."""
    contexto = trace.get_current_span(contexto_de(portador)).get_span_context()
    return [trace.Link(contexto)] if contexto.is_valid else []


def criar_provedor(ambiente: Mapping[str, str], *, processo: str) -> TracerProvider:
    """``TracerProvider`` do processo; com ``OTEL_ENABLED``, exporta por OTLP.

    O gRPC so e importado com a exportacao ligada: sem ela o provider serve ao
    contexto e ao ``trace_id`` do log e nao sai do processo.
    """
    provedor = TracerProvider(
        sampler=ParentBased(root=AmostradorDeRaiz()),
        resource=Resource.create(
            {
                "service.name": ambiente.get("OTEL_SERVICE_NAME") or _SERVICO_PADRAO,
                "service.version": ambiente.get("PYTSTOP_GIT_SHA", "unknown")[:12],
                "pytstop.processo": processo,
            }
        ),
    )
    if ambiente.get("OTEL_ENABLED", "").strip().lower() in _VERDADEIROS:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        endpoint = ambiente.get("OTEL_EXPORTER_OTLP_ENDPOINT") or _ENDPOINT_PADRAO
        provedor.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=endpoint, insecure=endpoint.startswith("http://")
                )
            )
        )
    return provedor


def instrumentar_clientes(provedor: TracerProvider) -> None:
    """Spans das consultas ao MongoDB e das chamadas httpx (Mercado Pago).

    Sem ``capture_statement``: o comando do MongoDB leva o documento inteiro, e
    os documentos do Billing levam o link de decisao e o ``checkout_url`` (tokens)
    e o valor cobrado. O span guarda so o nome do comando e a colecao. O PyMongo
    registra o ouvinte ao criar o cliente, entao isto roda antes do
    ``criar_cliente`` do processo.
    """
    PymongoInstrumentor().instrument(tracer_provider=provedor, capture_statement=False)
    HTTPXClientInstrumentor().instrument(tracer_provider=provedor)


def configurar_telemetria(processo: str) -> TracerProvider:
    """Instala o provider e a instrumentacao do processo; ``shutdown`` no fim
    descarrega os spans."""
    provedor = criar_provedor(os.environ, processo=processo)
    trace.set_tracer_provider(provedor)
    instrumentar_clientes(provedor)
    return provedor
