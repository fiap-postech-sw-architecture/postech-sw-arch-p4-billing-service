"""Headers de seguranca e propagacao do X-Request-ID.

Reaproveitado do p3 @ 08dcffe (``SecurityHeadersMiddleware`` de
``src/compartilhado/interfaces/middleware.py``). CORS, rate limiting e proxy
headers ficaram de fora: o Billing nao tem front-end no navegador e o rate
limiting das rotas publicas fica no Kong (RFC-004 §5).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response

_CSP_DEFAULT = "default-src 'none'"
# Swagger UI / ReDoc usam scripts e estilos inline que o CSP padrao bloqueia.
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")

# X-Request-ID vindo da borda (Kong, plugin correlation-id) e aceito quando
# "sano": ate 128 chars de charset seguro para log e header. Qualquer outra
# coisa e descartada e um uuid4 novo assume (sem injecao de log/header).
_REQUEST_ID_EXTERNO_VALIDO = re.compile(r"[A-Za-z0-9._=-]{1,128}")


def _caminho_de_docs(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _DOCS_PATHS)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Anexa headers de seguranca em toda resposta e propaga o request_id."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        recebido = request.headers.get("X-Request-ID", "")
        request_id = (
            recebido if _REQUEST_ID_EXTERNO_VALIDO.fullmatch(recebido) else str(uuid4())
        )
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        response.headers["Cache-Control"] = "no-store"
        if not _caminho_de_docs(request.url.path):
            response.headers["Content-Security-Policy"] = _CSP_DEFAULT
        response.headers["X-Request-ID"] = request_id
        return response
