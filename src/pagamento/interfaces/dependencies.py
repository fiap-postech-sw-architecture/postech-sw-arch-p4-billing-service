"""Composicao dos casos de uso do pagamento expostos na API."""

from __future__ import annotations

from starlette.requests import Request

from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.pagamento.aplicacao.use_cases import (
    ConsultarPagamentos,
    ProcessarNotificacaoPagamento,
    SimularResultadoPagamento,
)
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository


def obter_consultar_pagamentos(request: Request) -> ConsultarPagamentos:
    uow = MongoUnitOfWork(request.app.state.banco)
    return ConsultarPagamentos(MongoPagamentoRepository(uow))


def _processar(
    request: Request, uow: MongoUnitOfWork, pagamentos: MongoPagamentoRepository
) -> ProcessarNotificacaoPagamento:
    estado = request.app.state
    return ProcessarNotificacaoPagamento(
        uow,
        pagamentos,
        estado.gateway_pagamento,
        estado.metricas_pagamento,
        estado.config.pagamento_max_recusas,
        estado.relogio,
    )


def obter_processar_notificacao(request: Request) -> ProcessarNotificacaoPagamento:
    uow = MongoUnitOfWork(request.app.state.banco)
    return _processar(request, uow, MongoPagamentoRepository(uow))


def obter_simular_resultado(request: Request) -> SimularResultadoPagamento:
    # Rota so registrada com MP_MODE=simulado: o gateway e o simulador.
    uow = MongoUnitOfWork(request.app.state.banco)
    pagamentos = MongoPagamentoRepository(uow)
    return SimularResultadoPagamento(
        request.app.state.gateway_pagamento,
        pagamentos,
        _processar(request, uow, pagamentos),
        request.app.state.relogio,
    )
