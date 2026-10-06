from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from src.orcamento.dominio.orcamento import Orcamento


class OrcamentoRepository(Protocol):
    def obter_por_id(self, orcamento_id: UUID) -> Orcamento | None: ...

    def obter_por_ordem(self, ordem_id: UUID) -> Orcamento | None:
        """Ha no maximo um orcamento por ordem de servico."""
        ...

    def listar_vencidos(self, agora: datetime, limite: int) -> list[UUID]:
        """Ids dos pendentes com ``valido_ate`` anterior a ``agora``."""
        ...

    def salvar(self, orcamento: Orcamento) -> None:
        """Insere ou atualiza; outra ordem igual levanta ``OrcamentoJaGeradoError``."""
        ...
