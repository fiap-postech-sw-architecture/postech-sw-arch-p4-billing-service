"""Sondas da API (RFC-004, secao 6): liveness sem dependencias e readiness
so com o banco (o broker fora do ar nao tira a API do Service: a outbox segura
as mensagens)."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, status
from pymongo.errors import PyMongoError
from starlette.requests import Request

from src.compartilhado.infraestrutura.mongo import (
    BancoNaoPreparadoError,
    verificar_prontidao,
)

_log = structlog.get_logger(__name__)

router = APIRouter(tags=["saude"])


@router.get("/api/v1/saude", summary="Liveness (processo atende) e modo do pagamento")
async def saude(request: Request) -> dict[str, str]:
    """Responde 200 quando o processo atende, com o ``MP_MODE`` em uso.

    ``async`` de proposito (licao do p3): roda no event loop, fora do
    threadpool das rotas sync, e responde mesmo com o pool saturado. Nao
    consulta o MongoDB: uma queda do banco nao deve reiniciar os pods. O modo
    deixa visivel quando o simulador esta ligado (ADR-040).
    """
    return {"status": "ok", "modo": request.app.state.config.mp_modo.value}


@router.get(
    "/api/v1/saude/pronto",
    summary="Readiness: MongoDB responde e esta preparado",
    responses={503: {"description": "MongoDB fora do ar ou nao preparado."}},
)
def pronto(request: Request) -> dict[str, str]:
    """Ping e versao do banco em ate 2 s; 503 tira o pod do Service."""
    try:
        verificar_prontidao(request.app.state.banco)
    except (PyMongoError, BancoNaoPreparadoError) as exc:
        _log.warning("readiness_failed", erro=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Banco de dados indisponivel ou nao preparado",
        ) from None
    return {"status": "pronto"}
