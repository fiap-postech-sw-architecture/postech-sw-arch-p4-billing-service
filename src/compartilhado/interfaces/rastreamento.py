"""Spans HTTP da API (ADR-043): sem token nem id na URL e sem ruido de sonda.

O link de decisao do cliente leva o token no caminho (``/{token}`` e
``/{token}/decisao``) e o checkout do simulador leva o id do pagamento no
caminho e o token na query (ADR-039): a instrumentacao do FastAPI grava o
caminho e a URL crus em atributos do span, e o Jaeger os guardaria. O hook troca
tudo pelo template da rota casada; requisicao que nenhuma rota casou (um 404,
inclusive o do token com barra a mais) fica com ``nao_roteada``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit, urlunsplit

from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import ReadableSpan

from src.compartilhado.infraestrutura.metricas import ROTA_NAO_ROTEADA

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from fastapi import FastAPI
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.trace import Span

# Regex sobre a URL inteira: a raspagem do Prometheus e as sondas do kubelet
# (saude, viva, pronta) chegam a cada poucos segundos e nao sao trabalho.
URLS_SEM_TRACE: Final = "/metrics,/api/v1/saude"

_ROTA: Final = "http.route"
# Caminho: url.path (convencao nova) e http.target (antiga).
_ATRIBUTOS_DE_CAMINHO: Final = ("url.path", "http.target")
# URL inteira: url.full (convencao nova) e http.url (antiga).
_ATRIBUTOS_DE_URL: Final = ("url.full", "http.url")
_QUERY: Final = "url.query"
_QUERY_REDIGIDA: Final = "REDACTED"


def trocar_url_pelo_template(span: Span, _escopo: MutableMapping[str, Any]) -> None:
    """``server_request_hook``: roda depois de a instrumentacao gravar os
    atributos padrao do span, entao o que sobra nele e o template."""
    if not isinstance(span, ReadableSpan) or not span.is_recording():
        return
    atributos = span.attributes or {}
    rota = str(atributos.get(_ROTA, ROTA_NAO_ROTEADA))
    for nome in _ATRIBUTOS_DE_CAMINHO:
        if nome in atributos:
            span.set_attribute(nome, rota)
    for nome in _ATRIBUTOS_DE_URL:
        url = atributos.get(nome)
        if isinstance(url, str):
            partes = urlsplit(url)._replace(path=rota, query="", fragment="")
            span.set_attribute(nome, urlunsplit(partes))
    if _QUERY in atributos:
        span.set_attribute(_QUERY, _QUERY_REDIGIDA)


def instrumentar_api(app: FastAPI, provedor: TracerProvider) -> None:
    """Um span SERVER por requisicao, filho do ``traceparent`` que chegou.

    Chamado antes do primeiro request: a instrumentacao embrulha a pilha de
    middlewares do app na hora em que o Starlette a monta. Os spans de cada
    ``receive`` e ``send`` ASGI ficam de fora (varios por requisicao, sem
    informacao alem do status, que o SERVER ja leva). Com a instrumentacao no
    app, o FastAPI nao liga a telemetria nativa dele (nao duplica o span).
    """
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provedor,
        excluded_urls=URLS_SEM_TRACE,
        server_request_hook=trocar_url_pelo_template,
        exclude_spans=["receive", "send"],
    )
