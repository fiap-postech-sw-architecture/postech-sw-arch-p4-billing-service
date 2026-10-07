"""Evento de integracao: fato do Billing publicado aos outros servicos.

Todo evento do Billing cruza a fronteira do servico (catalogo da RFC-004,
secao 5.3)
e e entregue pela outbox transacional; nao ha consumidor in-process. Por isso
ha uma base unica, sem a separacao DomainEvent/IntegrationEvent do p3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class IntegrationEvent:
    """Base dos eventos do catalogo.

    Os campos da subclasse (``ordem_id`` incluso) sao exatamente o ``dados``
    da mensagem; o ``tipo`` e o nome da classe sem o sufixo ``Event``.
    ``ordem_id`` e o ``correlation_id`` da saga. O resto do envelope
    (``ocorrido_em``, pelo relogio injetado, e ``causation_id``) e de quem
    grava a outbox, na mesma transacao.
    """

    ordem_id: UUID

    @property
    def tipo(self) -> str:
        return type(self).__name__.removesuffix("Event")
