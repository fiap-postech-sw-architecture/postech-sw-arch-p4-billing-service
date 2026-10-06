from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.compartilhado.dominio.events import IntegrationEvent


class UnitOfWork(Protocol):
    """Transacao de um caso de uso, com outbox na mesma transacao.

    Difere do ``with uow: ...; uow.commit()`` do p3: a transacao do MongoDB
    e repetida inteira em conflito transitorio (``WriteConflict``), entao o
    trabalho chega como funcao reexecutavel. A cada tentativa os agregados sao
    relidos, o que transforma a corrida (decisao x expiracao) em releitura do
    valor atual, nao em sobrescrita.
    """

    def executar[T](self, trabalho: Callable[[], T]) -> T:
        """Roda ``trabalho`` numa transacao e grava no outbox, na mesma
        transacao, os eventos dos agregados salvos e os registrados avulsos.

        ``trabalho`` nao pode ter efeito fora do banco (pode rodar mais de uma
        vez). Excecoes de dominio abortam a transacao e propagam.
        """
        ...

    def registrar_evento(self, evento: IntegrationEvent) -> None:
        """Enfileira no outbox um evento sem agregado (so dentro de ``executar``)."""
        ...
