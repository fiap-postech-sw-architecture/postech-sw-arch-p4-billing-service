"""Metricas Prometheus da API, expostas em ``/metrics``.

Adaptado do p3 @ 08dcffe (``src/compartilhado/infraestrutura/metrics.py``):
mesmo nome-contrato com os dashboards (``http_request_duration_seconds``
com ``method``, ``rota`` e ``status``), mas direto no ``prometheus_client``,
sem o MeterProvider do OpenTelemetry (a observabilidade OTel entra no PR de
observabilidade da fase 4).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

from prometheus_client import CONTENT_TYPE_LATEST, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request

# Requests sem rota casada (404 de path desconhecido) agregam num unico valor
# em vez de explodir a cardinalidade com paths arbitrarios.
_ROTA_NAO_ROTEADA: Final = "nao_roteada"

HTTP_DURACAO = Histogram(
    "http_request_duration_seconds",
    "Duracao das requests HTTP da API por rota.",
    ["method", "rota", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)


class MetricasHTTPMiddleware(BaseHTTPMiddleware):
    """Observa a latencia de cada request; excecao nao tratada conta como 500."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        inicio = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            HTTP_DURACAO.labels(
                method=request.method,
                rota=_rota_template(request),
                status=str(status),
            ).observe(time.perf_counter() - inicio)


def _rota_template(request: Request) -> str:
    """Path template da rota casada (nunca o path bruto, que carrega ids)."""
    rota = request.scope.get("route")
    template = getattr(rota, "path_format", None) or getattr(rota, "path", None)
    return template if isinstance(template, str) else _ROTA_NAO_ROTEADA


def configurar_metricas(app: FastAPI) -> None:
    """Expoe ``GET /metrics`` e instala o middleware de latencia.

    Rota comum, e nao o ``make_asgi_app`` montado: o mount so responde em
    ``/metrics/`` e, sem o redirect de barra final, o scrape em ``/metrics``
    seria 404.
    """

    @app.get("/metrics", include_in_schema=False)
    def metricas() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.add_middleware(MetricasHTTPMiddleware)
