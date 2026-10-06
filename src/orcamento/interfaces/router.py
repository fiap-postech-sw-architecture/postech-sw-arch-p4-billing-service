"""Orcamentos para usuarios internos: consulta e decisao em nome do cliente."""

from __future__ import annotations

import dataclasses
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query

from src.compartilhado.interfaces.autenticacao import (
    Papel,
    UsuarioAutenticado,
    exigir_papel,
)
from src.orcamento.aplicacao.use_cases import ConsultarOrcamentos, DecidirOrcamento
from src.orcamento.interfaces.dependencies import (
    obter_consultar_orcamentos,
    obter_decidir_orcamento,
)
from src.orcamento.interfaces.schemas import DecisaoRequest, OrcamentoResponse

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/orcamentos", tags=["orcamentos"])

Atendente = Annotated[UsuarioAutenticado, Depends(exigir_papel(Papel.ATENDENTE))]
Consulta = Annotated[ConsultarOrcamentos, Depends(obter_consultar_orcamentos)]

_RESPOSTAS_DECISAO: dict[int | str, dict[str, object]] = {
    404: {"description": "Orcamento nao encontrado."},
    409: {"description": "Orcamento ja decidido, cancelado ou expirado."},
    410: {"description": "Prazo de decisao esgotado."},
}


@router.get(
    "/{orcamento_id}",
    summary="Consulta um orcamento",
    responses={404: {"description": "Orcamento nao encontrado."}},
)
def obter_orcamento(
    orcamento_id: UUID, _usuario: Atendente, consulta: Consulta
) -> OrcamentoResponse:
    return OrcamentoResponse.model_validate(
        dataclasses.asdict(consulta.por_id(orcamento_id))
    )


@router.get("", summary="Lista o orcamento de uma ordem de servico (0 ou 1)")
def listar_por_ordem(
    ordem_id: Annotated[UUID, Query()], _usuario: Atendente, consulta: Consulta
) -> list[OrcamentoResponse]:
    return [
        OrcamentoResponse.model_validate(dataclasses.asdict(dto))
        for dto in consulta.por_ordem(ordem_id)
    ]


@router.post(
    "/{orcamento_id}/decisao",
    summary="Aprova ou recusa o orcamento em nome do cliente (atendente)",
    responses=_RESPOSTAS_DECISAO,
)
def decidir_por_atendente(
    orcamento_id: UUID,
    body: DecisaoRequest,
    usuario: Atendente,
    decidir: Annotated[DecidirOrcamento, Depends(obter_decidir_orcamento)],
) -> OrcamentoResponse:
    dto = decidir.por_atendente(orcamento_id, aprovar=body.decisao == "aprovar")
    # Trilha de auditoria: quem decidiu em nome do cliente.
    _log.info(
        "orcamento_decidido_por_atendente",
        orcamento_id=str(orcamento_id),
        decisao=body.decisao,
        ator=usuario.sub,
    )
    return OrcamentoResponse.model_validate(dataclasses.asdict(dto))
