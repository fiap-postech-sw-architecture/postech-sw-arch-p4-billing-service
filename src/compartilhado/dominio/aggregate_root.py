from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.compartilhado.dominio.entity import Entity

if TYPE_CHECKING:
    from src.compartilhado.dominio.events import IntegrationEvent


@dataclass(eq=False)
class AggregateRoot(Entity):
    _eventos_pendentes: list[IntegrationEvent] = field(
        default_factory=list,
        init=False,
        repr=False,
    )

    def coletar_eventos(self) -> list[IntegrationEvent]:
        return list(self._eventos_pendentes)

    def limpar_eventos(self) -> None:
        self._eventos_pendentes.clear()

    def _registrar_evento(self, evento: IntegrationEvent) -> None:
        self._eventos_pendentes.append(evento)
