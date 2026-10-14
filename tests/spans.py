"""Oraculo dos testes de span: tudo o que o Jaeger guardaria."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.util.types import Attributes


def _pares(atributos: Attributes | Mapping[str, object]) -> list[str]:
    return [chave + "=" + str(valor) for chave, valor in (atributos or {}).items()]


def tudo_que_o_span_leva(span: ReadableSpan) -> list[str]:
    """Nome, atributos, eventos (nome e atributos), links, descricao do status e
    recurso do span, cada um como texto (``chave=valor`` nos que tem chave).

    Um teste de "nada de segredo no span" procura o segredo aqui: olhar so os
    atributos deixa passar o texto da excecao, que viaja no evento ``exception``
    e na descricao do status.
    """
    textos = [span.name]
    textos += _pares(span.attributes)
    if span.status.description:
        textos.append(span.status.description)
    for evento in span.events:
        textos.append(evento.name)
        textos += _pares(evento.attributes)
    for ligacao in span.links:
        textos += _pares(ligacao.attributes)
    textos += _pares(span.resource.attributes)
    return textos


def tudo_que_os_spans_levam(spans: Iterable[ReadableSpan]) -> list[str]:
    return [texto for span in spans for texto in tudo_que_o_span_leva(span)]
