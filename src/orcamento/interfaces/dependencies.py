"""Composicao dos casos de uso do orcamento expostos na API."""

from __future__ import annotations

from starlette.requests import Request

from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.orcamento.aplicacao.use_cases import ConsultarOrcamentos, DecidirOrcamento
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository


def obter_decidir_orcamento(request: Request) -> DecidirOrcamento:
    uow = MongoUnitOfWork(request.app.state.banco)
    return DecidirOrcamento(
        uow,
        MongoOrcamentoRepository(uow),
        request.app.state.link_decisao,
        request.app.state.relogio,
    )


def obter_consultar_orcamentos(request: Request) -> ConsultarOrcamentos:
    uow = MongoUnitOfWork(request.app.state.banco)
    return ConsultarOrcamentos(
        MongoOrcamentoRepository(uow),
        request.app.state.link_decisao,
        request.app.state.relogio,
    )
