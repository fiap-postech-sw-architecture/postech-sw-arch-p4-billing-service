from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class NotificacaoResponse(BaseModel):
    recebida_em: datetime
    referencia_pagamento: str
    status_provedor: str


class EstornoAutomaticoResponse(BaseModel):
    referencia_pagamento: str
    registrado_em: datetime
    falha: str | None


class PagamentoResponse(BaseModel):
    """Pagamento; campos da cobranca nulos so na lapide (compensacao que
    chegou antes do ``SolicitarPagamento``)."""

    id: UUID
    ordem_id: UUID
    status: str
    orcamento_id: UUID | None
    valor: Decimal | None
    moeda: str | None
    provedor: str | None
    referencia_preferencia: str | None
    checkout_url: str | None
    expira_em: datetime | None
    criado_em: datetime
    recusas: int
    referencia_pagamento: str | None
    confirmado_em: datetime | None
    encerrado_em: datetime | None
    motivo: str | None
    estornado_em: datetime | None
    motivo_estorno: str | None
    notificacoes: list[NotificacaoResponse]
    estornos_automaticos: list[EstornoAutomaticoResponse]


class NotificacaoMercadoPagoRequest(BaseModel):
    """Corpo do webhook: so o ``type`` e lido (quando a query nao o traz). O id
    a consultar vem do ``data.id`` da query, que a ``x-signature`` assina."""

    model_config = ConfigDict(extra="ignore")

    type: str | None = None


class WebhookResponse(BaseModel):
    processado: bool
