from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Collection

    from src.precos.dominio.preco import PrecoPeca, PrecoServico


class PrecoServicoRepository(Protocol):
    def obter_por_codigo(self, codigo: str) -> PrecoServico | None: ...

    def obter_por_codigos(self, codigos: Collection[str]) -> dict[str, PrecoServico]:
        """Precos encontrados, indexados por codigo (ausentes ficam de fora)."""
        ...

    def listar(self, *, offset: int, limit: int) -> list[PrecoServico]:
        """Pagina ordenada por codigo."""
        ...

    def contar(self) -> int: ...

    def salvar(self, preco: PrecoServico) -> None:
        """Insere ou atualiza; codigo repetido levanta ``PrecoJaCadastradoError``."""
        ...


class PrecoPecaRepository(Protocol):
    def obter_por_codigo(self, sku: str) -> PrecoPeca | None: ...

    def obter_por_codigos(self, skus: Collection[str]) -> dict[str, PrecoPeca]:
        """Precos encontrados, indexados por sku (ausentes ficam de fora)."""
        ...

    def listar(self, *, offset: int, limit: int) -> list[PrecoPeca]:
        """Pagina ordenada por sku."""
        ...

    def contar(self) -> int: ...

    def salvar(self, preco: PrecoPeca) -> None:
        """Insere ou atualiza; sku repetido levanta ``PrecoJaCadastradoError``."""
        ...
