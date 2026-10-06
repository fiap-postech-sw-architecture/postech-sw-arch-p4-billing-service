from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from src.pagamento.dominio.pagamento import Pagamento


class PagamentoRepository(Protocol):
    def obter_por_id(self, pagamento_id: UUID) -> Pagamento | None: ...

    def obter_por_orcamento(self, orcamento_id: UUID) -> Pagamento | None:
        """Ha no maximo um pagamento por orcamento."""
        ...

    def listar_vencidos(self, agora: datetime, limite: int) -> list[UUID]:
        """Ids dos pendentes com ``expira_em`` anterior a ``agora``."""
        ...

    def salvar(self, pagamento: Pagamento) -> None:
        """Insere ou atualiza.

        Outro pagamento do mesmo orcamento levanta ``PagamentoJaSolicitadoError``.
        """
        ...
