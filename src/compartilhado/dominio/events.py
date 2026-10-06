"""Evento de integracao: fato do Billing publicado aos outros servicos.

Todo evento do Billing cruza a fronteira do servico (catalogo da RFC-004,
secao 5.3)
e e entregue pela outbox transacional; nao ha consumidor in-process. Por isso
ha uma base unica, sem a separacao DomainEvent/IntegrationEvent do p3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.compartilhado.dominio.relogio import agora_utc

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class IntegrationEvent:
    """Base dos eventos do catalogo.

    Os campos da subclasse (``ordem_id`` incluso) sao exatamente o ``dados``
    da mensagem; o ``tipo`` e o nome da classe sem o sufixo ``Event``.
    ``ordem_id`` e o ``correlation_id`` da saga. ``ocorrido_em`` vai para o
    envelope e fica fora da igualdade, para os testes compararem eventos
    inteiros com ``==``.
    """

    ordem_id: UUID
    ocorrido_em: datetime = field(default_factory=agora_utc, compare=False)

    @property
    def tipo(self) -> str:
        return type(self).__name__.removesuffix("Event")
