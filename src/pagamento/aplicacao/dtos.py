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
class EstornoAutomaticoDTO:
    referencia_pagamento: str
    registrado_em: datetime
    falha: str | None


@dataclass(frozen=True, slots=True)
class PagamentoDTO:
    """Pagamento para a API; campos da cobranca nulos so na lapide."""

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
    notificacoes: tuple[NotificacaoDTO, ...]
    estornos_automaticos: tuple[EstornoAutomaticoDTO, ...]
    lapide: bool

    @classmethod
    def de(cls, pagamento: Pagamento) -> PagamentoDTO:
        cobranca = pagamento.cobranca
        preferencia = cobranca.referencia_preferencia if cobranca else None
        return cls(
            id=pagamento.id,
            ordem_id=pagamento.ordem_id,
            status=pagamento.status.value,
            orcamento_id=cobranca.orcamento_id if cobranca else None,
            valor=cobranca.valor.valor if cobranca else None,
            moeda=cobranca.valor.moeda if cobranca else None,
            provedor=cobranca.provedor if cobranca else None,
            referencia_preferencia=preferencia,
            checkout_url=cobranca.checkout_url if cobranca else None,
            expira_em=cobranca.expira_em if cobranca else None,
            criado_em=pagamento.criado_em,
            recusas=pagamento.recusas,
            referencia_pagamento=pagamento.referencia_pagamento,
            confirmado_em=pagamento.confirmado_em,
            encerrado_em=pagamento.encerrado_em,
            motivo=pagamento.motivo,
            estornado_em=pagamento.estornado_em,
            motivo_estorno=(
                pagamento.motivo_estorno.value if pagamento.motivo_estorno else None
            ),
            notificacoes=tuple(
                NotificacaoDTO(
                    recebida_em=n.recebida_em,
                    referencia_pagamento=n.referencia_pagamento,
                    status_provedor=n.status_provedor,
                )
                for n in pagamento.notificacoes
            ),
            estornos_automaticos=tuple(
                EstornoAutomaticoDTO(
                    referencia_pagamento=e.referencia_pagamento,
                    registrado_em=e.registrado_em,
                    falha=e.falha,
                )
                for e in pagamento.estornos_automaticos
            ),
            lapide=pagamento.e_lapide,
        )
