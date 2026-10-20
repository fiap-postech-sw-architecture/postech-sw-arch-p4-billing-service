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

# A raspagem do Prometheus e as sondas do kubelet (saude e saude/pronto) chegam a
# cada poucos segundos e nao sao trabalho. A instrumentacao testa a regex contra
# ``esquema://<Host do chamador><caminho>``, e o Host vem do chamador: uma regex
# solta (``/metrics``) casaria com ``Host: x/metrics`` e tiraria do trace a
# requisicao inteira. Ancorada, o ``$`` e o fim do caminho de verdade: o Host nao
# tem ``/`` nem ``?`` (nem ``#``) antes dele, so segmentos de caminho (o prefixo
# do ``ROOT_PATH``, como ``/billing``), e a rota termina a URL.
_INICIO_DA_URL: Final = r"^https?://[^/?#]+(?:/[^/?#]+)*"
URLS_SEM_TRACE: Final = (
    rf"{_INICIO_DA_URL}/metrics$,{_INICIO_DA_URL}/api/v1/saude(?:/pronto)?$"
)

_ROTA: Final = "http.route"
# Caminho: url.path (convencao nova) e http.target (antiga).
_ATRIBUTOS_DE_CAMINHO: Final = ("url.path", "http.target")
# URL inteira: url.full (convencao nova) e http.url (antiga).
_ATRIBUTOS_DE_URL: Final = ("url.full", "http.url")
_QUERY: Final = "url.query"
_REDIGIDO: Final = "REDACTED"
# O que o span SERVER troca por valor fixo (convencao antiga e nova). Nada disso
# serve a operacao, e o Jaeger guarda por contagem de traces, sem como apagar um
# titular. ``http.server_name`` repete o ``Host`` que o chamador manda, e a
# autoridade do ``http.url`` tambem (o ``_url_do_template`` a troca);
# ``http.host``, ``server.address`` e as portas do servidor sao o soquete do
# proprio pod, nao dado do chamador, e saem do span por escolha.
_TEXTOS_REDIGIDOS: Final = (
    # De quem chamou: o User-Agent (texto livre, sem limite) e o par da conexao
    # (no cluster, o pod do Kong).
    "http.user_agent",
    "user_agent.original",
    "net.peer.ip",
    "client.address",
    # O Host do chamador.
    "http.server_name",
    # O endereco do servidor.
    "http.host",
    "server.address",
    # Esta versao da instrumentacao nao os grava; ficam por defesa.
    "net.host.name",
    "net.host.ip",
)
_PORTAS_ZERADAS: Final = (
    "net.peer.port",
    "client.port",
    "net.host.port",
    "server.port",
)


def _substituicoes(rota: str) -> dict[str, str | int]:
    """O que troca cada atributo que o span tiver, na ordem em que a troca roda:
    a query primeiro, porque a URL (abaixo) e montada com o Host do chamador, e a
    instrumentacao engole o erro de um hook que levanta (o que vem depois nao
    roda, e o span sai com o valor cru)."""
    return {
        _QUERY: _REDIGIDO,
        **dict.fromkeys(_TEXTOS_REDIGIDOS, _REDIGIDO),
        **dict.fromkeys(_PORTAS_ZERADAS, 0),
        **dict.fromkeys(_ATRIBUTOS_DE_CAMINHO, rota),
    }


def trocar_url_pelo_template(span: Span, _escopo: MutableMapping[str, Any]) -> None:
    """``server_request_hook``: roda depois de a instrumentacao gravar os
    atributos padrao do span, entao o que sobra nele e o template."""
    if not isinstance(span, ReadableSpan) or not span.is_recording():
        return
    atributos = span.attributes or {}
    rota = str(atributos.get(_ROTA, ROTA_NAO_ROTEADA))
    for nome, valor in _substituicoes(rota).items():
        if nome in atributos:
            span.set_attribute(nome, valor)
    for nome in _ATRIBUTOS_DE_URL:
        url = atributos.get(nome)
        if isinstance(url, str):
            span.set_attribute(nome, _url_do_template(url, rota))


def _url_do_template(url: str, rota: str) -> str:
    try:
        partes = urlsplit(url)
    except ValueError:  # Host malformado (``[``, ``a]b``): nada a preservar
        return rota
    return urlunsplit(
        partes._replace(netloc=_REDIGIDO, path=rota, query="", fragment="")
    )


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
