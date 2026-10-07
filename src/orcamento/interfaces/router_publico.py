"""Link publico do cliente: consulta e decisao sem login, pelo token assinado.

O token e a credencial (HMAC com expiracao). Token adulterado ou expirado,
orcamento inexistente ou ja decidido: o mesmo 404 (ADR-039). Rate limiting
dessas rotas fica no Kong (ADR-038).
"""

from __future__ import annotations

import dataclasses
from typing import Annotated

from fastapi import APIRouter, Depends

from src.orcamento.aplicacao.link_decisao import CAMINHO_DO_LINK
from src.orcamento.aplicacao.use_cases import ConsultarOrcamentos, DecidirOrcamento
from src.orcamento.interfaces.dependencies import (
    obter_consultar_orcamentos,
    obter_decidir_orcamento,
)
from src.orcamento.interfaces.schemas import DecisaoRequest, OrcamentoPublicoResponse

PREFIXO = CAMINHO_DO_LINK

router = APIRouter(prefix=PREFIXO, tags=["publico"])

_RESPOSTAS_LINK: dict[int | str, dict[str, object]] = {
    404: {
        "description": (
            "Link invalido, expirado ou ja utilizado (mesma resposta para todos)."
        )
    },
}


@router.get(
    "/{token}",
    summary="Consulta o orcamento pelo link enviado ao cliente",
    responses=_RESPOSTAS_LINK,
)
def consultar_pelo_link(
    token: str,
    consulta: Annotated[ConsultarOrcamentos, Depends(obter_consultar_orcamentos)],
) -> OrcamentoPublicoResponse:
    return OrcamentoPublicoResponse.model_validate(
        dataclasses.asdict(consulta.por_link(token))
    )


@router.post(
    "/{token}/decisao",
    summary="Aprova ou recusa o orcamento pelo link enviado ao cliente",
    responses=_RESPOSTAS_LINK,
)
def decidir_pelo_link(
    token: str,
    body: DecisaoRequest,
    decidir: Annotated[DecidirOrcamento, Depends(obter_decidir_orcamento)],
) -> OrcamentoPublicoResponse:
    aprovado = body.decisao == "aprovar"
    dto = decidir.por_link(token, aprovar=aprovado)
    return OrcamentoPublicoResponse.model_validate(dataclasses.asdict(dto))
