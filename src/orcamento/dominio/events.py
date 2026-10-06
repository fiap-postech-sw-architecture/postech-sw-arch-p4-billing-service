"""Eventos do orcamento (catalogo da RFC-004 §4; campos = ``dados``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.compartilhado.dominio.events import IntegrationEvent

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal
    from uuid import UUID

    from src.orcamento.dominio.orcamento import CanalDecisao


@dataclass(frozen=True, slots=True)
class LinhaOrcamentoGerado:
    codigo: str
    descricao: str
    quantidade: int
    preco_unitario: Decimal
    subtotal: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class OrcamentoGeradoEvent(IntegrationEvent):
    orcamento_id: UUID
    linhas: tuple[LinhaOrcamentoGerado, ...]
    total: Decimal
    moeda: str
    valido_ate: datetime
    link_decisao: str


@dataclass(frozen=True, slots=True, kw_only=True)
class GeracaoDeOrcamentoFalhouEvent(IntegrationEvent):
    motivo: str
    codigos_invalidos: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class OrcamentoAprovadoEvent(IntegrationEvent):
    orcamento_id: UUID
    decidido_em: datetime
    canal: CanalDecisao


@dataclass(frozen=True, slots=True, kw_only=True)
class OrcamentoRecusadoEvent(IntegrationEvent):
    orcamento_id: UUID
    decidido_em: datetime
    canal: CanalDecisao


@dataclass(frozen=True, slots=True, kw_only=True)
class OrcamentoExpiradoEvent(IntegrationEvent):
    orcamento_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class OrcamentoCanceladoEvent(IntegrationEvent):
    orcamento_id: UUID
