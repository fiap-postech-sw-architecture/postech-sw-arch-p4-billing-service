from __future__ import annotations

from fastapi import APIRouter
from starlette.requests import Request

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
