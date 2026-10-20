"""OpenTelemetry do Billing: contexto W3C na outbox e nos headers AMQP (ADR-043).

API, relay, consumidor e ``prazos`` instalam o ``TracerProvider`` do SDK sempre,
para o contexto seguir de mensagem em mensagem e o ``trace_id`` chegar ao log; a
exportacao OTLP/gRPC so liga com ``OTEL_ENABLED=true`` (endpoint em
``OTEL_EXPORTER_OTLP_ENDPOINT``), os mesmos nomes do contrato de configuracao da
plataforma. O evento espontaneo segue o contexto guardado no registro que
esperava por ele (``aberto_por``, na unidade de trabalho), com span link para o
span de quem retomou o passo: a requisicao da API ou o ciclo do ``prazos``.

Dois cuidados valem para todo span do provider, venha do codigo do servico ou da
instrumentacao do FastAPI, do PyMongo e do httpx:

- o amostrador descarta o span de cliente (consulta ao MongoDB, chamada HTTP) que
  nasce sem pai: e o poll do relay, a consulta do coletor de metricas e o ciclo
  ocioso do ``prazos``, que nao devem encher o Jaeger. Span de cliente dentro de
  um trace (requisicao, mensagem, ciclo com trabalho) segue o pai;
- nenhum span leva o texto de uma excecao nem a descricao do status de erro
  (``_SemTextoDeErro``): o texto traz o que a excecao carrega, do documento que
  o driver recusou ao cabecalho ``Authorization`` que o httpx nao enviou. O
  motivo da falha vai no atributo ``pytstop.falha``, so com o nome da classe.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit, urlunsplit

from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    Sampler,
    SamplingResult,
)
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from opentelemetry.context import Context
    from opentelemetry.trace import Link, Span, TraceState
    from opentelemetry.util.types import Attributes

# So traceparent/tracestate: o que o envelope do contrato carrega (sem baggage).
_PROPAGADOR: Final = TraceContextTextMapPropagator()
_VERDADEIROS: Final = frozenset({"true", "1"})
_TAMANHO_MAXIMO_TRACESTATE: Final = 512
# Atributos do evento ``exception`` que carregam o texto da excecao (o tipo fica).
_TEXTO_DA_EXCECAO: Final = frozenset({"exception.message", "exception.stacktrace"})
# Motivo da falha de um span: o nome da classe da excecao, nunca o texto dela.
ATRIBUTO_FALHA: Final = "pytstop.falha"
# Chamadas do gateway ao Mercado Pago, com o id no lugar do trecho variavel. O id
# da preferencia compoe o link do checkout, o do pagamento e credencial (ADR-039)
# e o ``external_reference`` da busca e o id do pagamento. A ordem importa: o
# ``search`` vem antes do padrao que casa qualquer segmento depois de ``payments``.
_ROTAS_DO_MERCADO_PAGO: Final = (
    (re.compile(r"/checkout/preferences"), "/checkout/preferences"),
    (re.compile(r"/checkout/preferences/[^/]+"), "/checkout/preferences/{id}"),
    (re.compile(r"/v1/payments/search"), "/v1/payments/search"),
    (re.compile(r"/v1/payments/[^/]+/refunds"), "/v1/payments/{id}/refunds"),
    (re.compile(r"/v1/payments/[^/]+"), "/v1/payments/{id}"),
)
# Caminho fora da lista (uma rota nova do gateway): pode trazer um id, entao sai
# um valor fixo, o mesmo ``nao_roteada`` do span SERVER.
_ROTA_DESCONHECIDA: Final = "nao_roteada"
# URL inteira: url.full (convencao nova) e http.url (antiga).
_ATRIBUTOS_DE_URL: Final = ("url.full", "http.url")
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


def _sem_tracestate_longo(portador: dict[str, str]) -> dict[str, str]:
    # Limite do W3C Trace Context: o header vem de fora e seria regravado em cada
    # outbox e copia de retry; o ``traceparent`` segue valendo.
    if len(portador.get("tracestate", "")) > _TAMANHO_MAXIMO_TRACESTATE:
        del portador["tracestate"]
    return portador


def contexto_atual() -> dict[str, str]:
    """``traceparent``/``tracestate`` do span corrente; vazio fora de span.

    ``tracestate`` acima de 512 caracteres e descartado aqui tambem: a requisicao
    HTTP chega pela instrumentacao do FastAPI, que le o header sem passar por
    ``contexto_de``, e o valor iria para a outbox e para o AMQP.
    """
    portador: dict[str, str] = {}
    _PROPAGADOR.inject(portador)
    return _sem_tracestate_longo(portador)


def contexto_de(portador: Mapping[str, object]) -> Context:
    """Contexto W3C lido de headers AMQP ou de um documento da outbox
    (``tracestate`` acima de 512 caracteres e descartado)."""
    textos = {
        chave: valor for chave, valor in portador.items() if isinstance(valor, str)
    }
    return _PROPAGADOR.extract(_sem_tracestate_longo(textos))


def links_de(portador: Mapping[str, object]) -> list[trace.Link]:
    """Span link para o contexto W3C do portador, quando ele e valido (ADR-043:
    o evento de um passo retomado aponta para o trace de quem o retomou)."""
    contexto = trace.get_current_span(contexto_de(portador)).get_span_context()
    return [trace.Link(contexto)] if contexto.is_valid else []


def _sem_texto_de_excecao(evento: Event) -> Event:
    if evento.name != "exception":
        return evento
    atributos = {
        chave: valor
        for chave, valor in (evento.attributes or {}).items()
        if chave not in _TEXTO_DA_EXCECAO
    }
    return Event(evento.name, atributos, evento.timestamp)


class _SemTextoDeErro(SpanProcessor):
    """Tira do span o texto de erro, que pode trazer dado do pedido.

    A mensagem e o stack trace saem de todo evento ``exception`` (fica o tipo), e
    o status de erro perde a descricao: o ``with`` do SDK e a instrumentacao do
    httpx e do PyMongo a preenchem com o texto da excecao ou da resposta.

    Primeiro processador do provider: roda antes de qualquer exportador, sobre a
    copia do span que todos recebem. O ``ReadableSpan`` nao tem setter, entao o
    processador troca os atributos protegidos ``_status`` e ``_events`` da copia
    (o span vivo ja terminou); os testes de cada regra reprovam se o SDK mudar
    isso.
    """

    def on_end(self, span: ReadableSpan) -> None:
        if span.status.description:
            span._status = Status(span.status.status_code)
        if any(evento.name == "exception" for evento in span.events):
            span._events = tuple(
                _sem_texto_de_excecao(evento) for evento in span.events
            )


def marcar_falha(span: Span, erro: BaseException) -> None:
    """Span com status de erro e o motivo em ``pytstop.falha``: o nome da classe
    da excecao, nunca o texto dela (o processador tira o texto do status)."""
    span.set_status(Status(StatusCode.ERROR))
    span.set_attribute(ATRIBUTO_FALHA, type(erro).__name__)


def criar_provedor(ambiente: Mapping[str, str], *, processo: str) -> TracerProvider:
    """``TracerProvider`` do processo; com ``OTEL_ENABLED``, exporta por OTLP.

    O gRPC so e importado com a exportacao ligada: sem ela o provider serve ao
    contexto e ao ``trace_id`` do log e nao sai do processo. O processador que
    tira o texto de erro entra primeiro, antes de qualquer exportador.
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
    provedor.add_span_processor(_SemTextoDeErro())
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


def _url_do_template(url: str) -> str:
    try:
        partes = urlsplit(url)
    except ValueError:  # URL malformada: nada a preservar
        return _ROTA_DESCONHECIDA
    rota = next(
        (
            template
            for padrao, template in _ROTAS_DO_MERCADO_PAGO
            if padrao.fullmatch(partes.path)
        ),
        _ROTA_DESCONHECIDA,
    )
    return urlunsplit(partes._replace(path=rota, query="", fragment=""))


def trocar_url_do_cliente_pelo_template(span: Span, _requisicao: object) -> None:
    """``request_hook`` do httpx: roda logo depois de a instrumentacao gravar a URL
    no span, entao o que sobra nele e o template. A instrumentacao nao protege o
    hook (o do FastAPI ela protege): um erro aqui derrubaria a chamada ao
    provedor, e este nao levanta."""
    if not isinstance(span, ReadableSpan):
        return
    atributos = span.attributes or {}
    for nome in _ATRIBUTOS_DE_URL:
        url = atributos.get(nome)
        if isinstance(url, str):
            span.set_attribute(nome, _url_do_template(url))


def instrumentar_clientes(provedor: TracerProvider) -> None:
    """Spans das consultas ao MongoDB e das chamadas httpx (Mercado Pago).

    Sem ``capture_statement``: o comando do MongoDB leva o documento inteiro, e
    os documentos do Billing levam o link de decisao e o ``checkout_url`` (tokens)
    e o valor cobrado. O span guarda so o nome do comando e a colecao. O PyMongo
    registra o ouvinte ao criar o cliente, entao isto roda antes do
    ``criar_cliente`` do processo.

    A URL do span do httpx leva o id da preferencia, o do pagamento e a query da
    busca: o hook a troca pelo template da rota.
    """
    PymongoInstrumentor().instrument(tracer_provider=provedor, capture_statement=False)
    HTTPXClientInstrumentor().instrument(
        tracer_provider=provedor, request_hook=trocar_url_do_cliente_pelo_template
    )


def instalar_propagador() -> None:
    """So ``traceparent`` e ``tracestate`` entram e saem do processo: o padrao do
    SDK soma o ``baggage``, que a API aceitaria do chamador e o httpx repassaria
    ao Mercado Pago."""
    set_global_textmap(_PROPAGADOR)


def configurar_telemetria(processo: str) -> TracerProvider:
    """Instala o provider, o propagador e a instrumentacao do processo;
    ``shutdown`` no fim descarrega os spans."""
    provedor = criar_provedor(os.environ, processo=processo)
    trace.set_tracer_provider(provedor)
    instalar_propagador()
    instrumentar_clientes(provedor)
    return provedor
