"""Porta de saida do orcamento para a tabela de precos (contexto vizinho).

Definida aqui (no consumidor) e implementada em ``infraestrutura`` do proprio
orcamento, como no p3: o adapter traduz o modelo de precos para estes DTOs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from src.compartilhado.dominio.dinheiro import Dinheiro


@dataclass(frozen=True, slots=True)
class PrecoCotado:
    descricao: str
    preco_unitario: Dinheiro


@dataclass(frozen=True, slots=True)
class Cotacao:
    """Precos vigentes dos codigos validos e a lista dos invalidos."""

    servicos: Mapping[str, PrecoCotado]
    pecas: Mapping[str, PrecoCotado]
    invalidos: tuple[str, ...]


class TabelaDePrecosPort(Protocol):
    def cotar(self, *, servicos: Collection[str], pecas: Collection[str]) -> Cotacao:
        """Cota na mesma transacao do orcamento; inativo conta como invalido."""
        ...
