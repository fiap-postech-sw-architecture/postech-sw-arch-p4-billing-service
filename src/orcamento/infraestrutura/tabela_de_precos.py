"""Adapter da ``TabelaDePrecosPort``: le os precos pelos repositorios do contexto
``precos`` na mesma unidade de trabalho (mesma transacao do orcamento)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.orcamento.aplicacao.ports import Cotacao, PrecoCotado
from src.precos.dominio.validacao import codigos_invalidos
from src.precos.infraestrutura.repository import (
    MongoPrecoPecaRepository,
    MongoPrecoServicoRepository,
)

if TYPE_CHECKING:
    from collections.abc import Collection

    from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork


class TabelaDePrecosMongoAdapter:
    def __init__(self, uow: MongoUnitOfWork) -> None:
        self._servicos = MongoPrecoServicoRepository(uow)
        self._pecas = MongoPrecoPecaRepository(uow)

    def cotar(self, *, servicos: Collection[str], pecas: Collection[str]) -> Cotacao:
        precos_servicos = self._servicos.obter_por_codigos(servicos)
        precos_pecas = self._pecas.obter_por_codigos(pecas)
        invalidos = codigos_invalidos(
            servicos_solicitados=servicos,
            pecas_solicitadas=pecas,
            servicos=precos_servicos,
            pecas=precos_pecas,
        )
        return Cotacao(
            servicos={
                codigo: PrecoCotado(descricao=p.nome, preco_unitario=p.preco)
                for codigo, p in precos_servicos.items()
                if p.ativo
            },
            pecas={
                sku: PrecoCotado(descricao=p.nome, preco_unitario=p.preco)
                for sku, p in precos_pecas.items()
                if p.ativo
            },
            invalidos=tuple(invalidos),
        )
