from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class LinhaResponse(BaseModel):
    tipo: str
    codigo: str
    descricao: str
    quantidade: int
    preco_unitario: Decimal
    subtotal: Decimal


class DecisaoResponse(BaseModel):
    canal: str
    decidido_em: datetime
    decidido_por: str | None = None


class OrcamentoResponse(BaseModel):
    id: UUID
    ordem_id: UUID
    status: str
    linhas: list[LinhaResponse]
    total: Decimal
    moeda: str
    criado_em: datetime
    valido_ate: datetime | None
    decisao: DecisaoResponse | None
    motivo_cancelamento: str | None


class OrcamentoPublicoResponse(BaseModel):
    """Projecao para o cliente via link: sem dados internos da ordem."""

    id: UUID
    status: str
    linhas: list[LinhaResponse]
    total: Decimal
    moeda: str
    valido_ate: datetime
    decisao: DecisaoResponse | None


class DecisaoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisao: Literal["aprovar", "recusar"]
