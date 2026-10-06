from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal
    from uuid import UUID

    from src.orcamento.dominio.orcamento import Orcamento, TipoItem


@dataclass(frozen=True, slots=True)
class ItemSolicitado:
    """Item do diagnostico a orcar (``GerarOrcamento.itens`` da RFC-004)."""

    tipo: TipoItem
    codigo: str
    quantidade: int


@dataclass(frozen=True, slots=True)
class LinhaDTO:
    tipo: str
    codigo: str
    descricao: str
    quantidade: int
    preco_unitario: Decimal
    subtotal: Decimal


@dataclass(frozen=True, slots=True)
class DecisaoDTO:
    canal: str
    decidido_em: datetime


@dataclass(frozen=True, slots=True)
class OrcamentoDTO:
    id: UUID
    ordem_id: UUID
    status: str
    linhas: tuple[LinhaDTO, ...]
    total: Decimal
    moeda: str
    criado_em: datetime
    # Nulo so na lapide (cancelamento que chegou antes da geracao).
    valido_ate: datetime | None
    decisao: DecisaoDTO | None
    motivo_cancelamento: str | None

    @classmethod
    def de(cls, orcamento: Orcamento) -> OrcamentoDTO:
        decisao = orcamento.decisao
        total = orcamento.total
        return cls(
            id=orcamento.id,
            ordem_id=orcamento.ordem_id,
            status=orcamento.status.value,
            linhas=tuple(
                LinhaDTO(
                    tipo=linha.tipo.value,
                    codigo=linha.codigo,
                    descricao=linha.descricao,
                    quantidade=linha.quantidade,
                    preco_unitario=linha.preco_unitario.valor,
                    subtotal=linha.subtotal.valor,
                )
                for linha in orcamento.linhas
            ),
            total=total.valor,
            moeda=total.moeda,
            criado_em=orcamento.criado_em,
            valido_ate=orcamento.valido_ate,
            decisao=(
                DecisaoDTO(canal=decisao.canal.value, decidido_em=decisao.decidido_em)
                if decisao
                else None
            ),
            motivo_cancelamento=orcamento.motivo_cancelamento,
        )
