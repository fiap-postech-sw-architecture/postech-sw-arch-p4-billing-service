from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class NotificacaoResponse(BaseModel):
    recebida_em: datetime
    referencia_pagamento: str
    status_provedor: str


class PagamentoResponse(BaseModel):
    id: UUID
    ordem_id: UUID
    orcamento_id: UUID
    valor: Decimal
    moeda: str
    status: str
    provedor: str
    referencia_preferencia: str
    referencia_pagamento: str | None
    checkout_url: str
    criado_em: datetime
    expira_em: datetime
    confirmado_em: datetime | None
    estornado_em: datetime | None
    motivo: str | None
    notificacoes: list[NotificacaoResponse]


class DadosDaNotificacao(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | int | None = None


class NotificacaoMercadoPagoRequest(BaseModel):
    """Corpo do webhook; so diz o que consultar (o status vem da consulta)."""

    model_config = ConfigDict(extra="ignore")

    type: str | None = None
    action: str | None = None
    data: DadosDaNotificacao | None = None


class WebhookResponse(BaseModel):
    processado: bool
