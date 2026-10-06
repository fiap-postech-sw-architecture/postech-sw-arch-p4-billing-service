from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["saude"])


@router.get("/api/v1/saude", summary="Liveness/readiness")
async def saude() -> dict[str, str]:
    """Responde 200 quando o processo atende.

    ``async`` de proposito (licao do p3): roda no event loop, fora do
    threadpool das rotas sync, e responde mesmo com o pool saturado. Nao
    consulta o MongoDB: uma queda do banco nao deve reiniciar os pods.
    """
    return {"status": "ok"}
