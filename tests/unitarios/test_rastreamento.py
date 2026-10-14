"""Hook que tira o token e o id da URL do span da requisicao (ADR-039, ADR-043)."""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from src.compartilhado.interfaces.rastreamento import (
    URLS_SEM_TRACE,
    trocar_url_pelo_template,
)

LINK_FICTICIO = (
    "0e3e4a2b-3c0a-4f5e-8d6e-1b2c3d4e5f60.4102444800.c2VnbWVudG8tYXNzaW5hZG8"
)
ROTA = "/api/v1/publico/orcamentos/{token}/decisao"
CAMINHO = f"/api/v1/publico/orcamentos/{LINK_FICTICIO}/decisao"


def novo_span(atributos: dict[str, Any]) -> trace.Span:
    """Span gravando, como o que a instrumentacao entrega ao hook."""
    return TracerProvider().get_tracer("teste").start_span("POST", attributes=atributos)


def atributos_de(span: trace.Span) -> dict[str, Any]:
    assert isinstance(span, ReadableSpan)
    return dict(span.attributes or {})


def test_convencao_antiga_troca_o_alvo_e_a_url_pelo_template() -> None:
    span = novo_span(
        {
            "http.route": ROTA,
            "http.method": "POST",
            "http.target": CAMINHO,
            "http.url": f"http://billing.teste:8000{CAMINHO}?a=1",
        }
    )

    trocar_url_pelo_template(span, {})

    assert atributos_de(span) == {
        "http.route": ROTA,
        "http.method": "POST",
        "http.target": ROTA,
        "http.url": f"http://billing.teste:8000{ROTA}",
    }


def test_convencao_nova_troca_caminho_url_e_query() -> None:
    span = novo_span(
        {
            "http.route": "/simulador/checkout/{pagamento_id}",
            "url.path": "/simulador/checkout/9f0c1a2b-1111-4222-8333-444455556666",
            "url.query": "token=segredo",
            "url.full": "http://billing.teste/simulador/checkout/9f0c?token=segredo",
        }
    )

    trocar_url_pelo_template(span, {})

    assert atributos_de(span) == {
        "http.route": "/simulador/checkout/{pagamento_id}",
        "url.path": "/simulador/checkout/{pagamento_id}",
        "url.query": "REDACTED",
        "url.full": "http://billing.teste/simulador/checkout/{pagamento_id}",
    }


def test_sem_rota_casada_nada_do_caminho_cru_fica_no_span() -> None:
    """Um 404 como o do token seguido de barra a mais: o caminho tem o token."""
    span = novo_span(
        {
            "http.target": f"{CAMINHO}/",
            "http.url": f"http://billing.teste{CAMINHO}/",
            "url.path": f"{CAMINHO}/",
            "url.full": f"https://billing.teste{CAMINHO}/?x=1",
            "url.query": "x=1",
        }
    )

    trocar_url_pelo_template(span, {})

    atributos = atributos_de(span)
    assert atributos == {
        "http.target": "nao_roteada",
        "http.url": "http://billing.teste/nao_roteada",
        "url.path": "nao_roteada",
        "url.full": "https://billing.teste/nao_roteada",
        "url.query": "REDACTED",
    }
    assert LINK_FICTICIO not in str(atributos)


def test_nao_cria_atributo_que_a_instrumentacao_nao_gravou() -> None:
    span = novo_span({"http.route": ROTA, "http.method": "POST"})

    trocar_url_pelo_template(span, {})

    assert atributos_de(span) == {"http.route": ROTA, "http.method": "POST"}


def test_url_que_nao_e_texto_fica_como_esta() -> None:
    span = novo_span({"http.route": ROTA, "http.url": 42})

    trocar_url_pelo_template(span, {})

    assert atributos_de(span)["http.url"] == 42


def test_span_sem_gravacao_e_ignorado() -> None:
    """Span descartado pelo amostrador (``NonRecordingSpan``) nao tem atributos."""
    contexto = SpanContext(1, 1, is_remote=False, trace_flags=TraceFlags(0))
    trocar_url_pelo_template(NonRecordingSpan(contexto), {})


def test_span_que_grava_mas_nao_e_do_sdk_e_ignorado() -> None:
    """Outro SDK de tracing (um fornecedor, um proxy) entrega um span que grava e
    nao tem ``attributes``: o hook nao pode quebrar a requisicao."""

    class SpanDeOutroSdk(NonRecordingSpan):
        def is_recording(self) -> bool:
            return True

    contexto = SpanContext(1, 1, is_remote=False, trace_flags=TraceFlags(1))
    trocar_url_pelo_template(SpanDeOutroSdk(contexto), {})


def test_span_ja_encerrado_nao_e_mexido() -> None:
    span = novo_span({"http.route": ROTA, "http.target": CAMINHO})
    span.end()

    trocar_url_pelo_template(span, {})

    assert atributos_de(span)["http.target"] == CAMINHO


@pytest.mark.parametrize(
    "url",
    [
        "http://billing:8000/metrics",
        "http://billing:8000/api/v1/saude",
        "http://billing:8000/api/v1/saude/pronto",
        "http://billing:8000/billing/metrics",
    ],
)
def test_sondas_e_raspagem_ficam_fora_do_trace(url: str) -> None:
    from opentelemetry.util.http import parse_excluded_urls

    assert parse_excluded_urls(URLS_SEM_TRACE).url_disabled(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://billing:8000/api/v1/orcamentos",
        "http://billing:8000/api/v1/publico/orcamentos/token",
        "http://billing:8000/simulador/checkout/9f0c",
    ],
)
def test_demais_rotas_seguem_no_trace(url: str) -> None:
    from opentelemetry.util.http import parse_excluded_urls

    assert not parse_excluded_urls(URLS_SEM_TRACE).url_disabled(url)
