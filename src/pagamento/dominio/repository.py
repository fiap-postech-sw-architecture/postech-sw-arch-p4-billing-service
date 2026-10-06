from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from src.pagamento.dominio.pagamento import Pagamento


class PagamentoRepository(Protocol):
    def obter_por_id(self, pagamento_id: UUID) -> Pagamento | None: ...

    def obter_por_ordem(self, ordem_id: UUID) -> Pagamento | None:
        """Ha no maximo um pagamento (ou lapide) por ordem de servico: e por
        aqui que a compensacao acha a cobranca."""
        ...

    def listar_vencidos(self, agora: datetime, limite: int) -> list[UUID]:
        """Ids dos solicitados com ``expira_em`` anterior a ``agora``."""
        ...

    def salvar(self, pagamento: Pagamento) -> None:
        """Insere ou atualiza.

        Outro pagamento da mesma ordem (ou do mesmo orcamento) levanta
        ``PagamentoJaSolicitadoError``.
        """
        ...
