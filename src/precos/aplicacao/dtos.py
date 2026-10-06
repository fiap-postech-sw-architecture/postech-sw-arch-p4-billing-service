from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal

    from src.precos.dominio.preco import PrecoPeca, PrecoServico


@dataclass(frozen=True, slots=True)
class PrecoServicoDTO:
    codigo: str
    nome: str
    descricao: str
    preco: Decimal
    moeda: str
    ativo: bool

    @classmethod
    def de(cls, preco: PrecoServico) -> PrecoServicoDTO:
        return cls(
            codigo=preco.codigo,
            nome=preco.nome,
            descricao=preco.descricao,
            preco=preco.preco.valor,
            moeda=preco.preco.moeda,
            ativo=preco.ativo,
        )


@dataclass(frozen=True, slots=True)
class PrecoPecaDTO:
    sku: str
    nome: str
    preco: Decimal
    moeda: str
    ativo: bool

    @classmethod
    def de(cls, preco: PrecoPeca) -> PrecoPecaDTO:
        return cls(
            sku=preco.sku,
            nome=preco.nome,
            preco=preco.preco.valor,
            moeda=preco.preco.moeda,
            ativo=preco.ativo,
        )


@dataclass(frozen=True, slots=True)
class Pagina[T]:
    itens: list[T]
    total: int
    offset: int
    limit: int
