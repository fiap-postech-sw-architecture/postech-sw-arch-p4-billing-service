"""Eventos do pagamento (catalogo da RFC-004 §4; campos = ``dados``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.compartilhado.dominio.events import IntegrationEvent

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal
    from uuid import UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class PagamentoSolicitadoEvent(IntegrationEvent):
    pagamento_id: UUID
    valor: Decimal
    moeda: str
    checkout_url: str
    expira_em: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class PagamentoConfirmadoEvent(IntegrationEvent):
    pagamento_id: UUID
    valor: Decimal
    moeda: str
    confirmado_em: datetime
    referencia_provedor: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PagamentoRecusadoEvent(IntegrationEvent):
    pagamento_id: UUID
    motivo: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PagamentoExpiradoEvent(IntegrationEvent):
    pagamento_id: UUID
    motivo: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PagamentoEstornadoEvent(IntegrationEvent):
    pagamento_id: UUID
    estornado_em: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class EstornoDePagamentoFalhouEvent(IntegrationEvent):
    pagamento_id: UUID
    motivo: str
