"""Adapter da ``OrcamentosPort``: le o agregado do contexto ``orcamento``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.orcamento.dominio.orcamento import StatusOrcamento
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.pagamento.aplicacao.ports import ItemCobranca, OrcamentoParaPagamento

if TYPE_CHECKING:
    from uuid import UUID

    from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork


class OrcamentosMongoAdapter:
    def __init__(self, uow: MongoUnitOfWork) -> None:
        self._orcamentos = MongoOrcamentoRepository(uow)

    def obter(self, orcamento_id: UUID) -> OrcamentoParaPagamento | None:
        orcamento = self._orcamentos.obter_por_id(orcamento_id)
        if orcamento is None:
            return None
        return OrcamentoParaPagamento(
            ordem_id=orcamento.ordem_id,
            aprovado=orcamento.status is StatusOrcamento.APROVADO,
            itens=tuple(
                ItemCobranca(
                    codigo=linha.codigo,
                    descricao=linha.descricao,
                    quantidade=linha.quantidade,
                    preco_unitario=linha.preco_unitario,
                )
                for linha in orcamento.linhas
            ),
            total=orcamento.total,
        )
