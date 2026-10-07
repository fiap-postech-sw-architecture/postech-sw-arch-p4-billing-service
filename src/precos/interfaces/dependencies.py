"""Composicao dos casos de uso de precos (uma unidade de trabalho por request)."""

from __future__ import annotations

from starlette.requests import Request

from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.precos.aplicacao.use_cases import PrecosDePecas, PrecosDeServicos, ValidarItens
from src.precos.infraestrutura.repository import (
    MongoPrecoPecaRepository,
    MongoPrecoServicoRepository,
)


def obter_precos_de_servicos(request: Request) -> PrecosDeServicos:
    uow = MongoUnitOfWork(request.app.state.banco)
    return PrecosDeServicos(uow, MongoPrecoServicoRepository(uow))


def obter_precos_de_pecas(request: Request) -> PrecosDePecas:
    uow = MongoUnitOfWork(request.app.state.banco)
    return PrecosDePecas(uow, MongoPrecoPecaRepository(uow))


def obter_validar_itens(request: Request) -> ValidarItens:
    uow = MongoUnitOfWork(request.app.state.banco)
    return ValidarItens(MongoPrecoServicoRepository(uow), MongoPrecoPecaRepository(uow))
