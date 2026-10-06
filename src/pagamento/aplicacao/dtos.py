from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal
    from uuid import UUID

    from src.pagamento.dominio.pagamento import Pagamento


@dataclass(frozen=True, slots=True)
class NotificacaoDTO:
    recebida_em: datetime
    referencia_pagamento: str
    status_provedor: str


@dataclass(frozen=True, slots=True)
class PagamentoDTO:
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
    notificacoes: tuple[NotificacaoDTO, ...]

    @classmethod
    def de(cls, pagamento: Pagamento) -> PagamentoDTO:
        return cls(
            id=pagamento.id,
            ordem_id=pagamento.ordem_id,
            orcamento_id=pagamento.orcamento_id,
            valor=pagamento.valor.valor,
            moeda=pagamento.valor.moeda,
            status=pagamento.status.value,
            provedor=pagamento.provedor,
            referencia_preferencia=pagamento.referencia_preferencia,
            referencia_pagamento=pagamento.referencia_pagamento,
            checkout_url=pagamento.checkout_url,
            criado_em=pagamento.criado_em,
            expira_em=pagamento.expira_em,
            confirmado_em=pagamento.confirmado_em,
            estornado_em=pagamento.estornado_em,
            motivo=pagamento.motivo,
            notificacoes=tuple(
                NotificacaoDTO(
                    recebida_em=n.recebida_em,
                    referencia_pagamento=n.referencia_pagamento,
                    status_provedor=n.status_provedor,
                )
                for n in pagamento.notificacoes
            ),
        )
