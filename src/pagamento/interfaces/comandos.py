"""Comandos da saga do pagamento, chamados pelo consumidor de ``billing.comandos``.

Traduzem o ``dados`` do contrato (ja validado) para os casos de uso, como os
routers fazem com o HTTP. A unidade de trabalho e a da mensagem: o comando e a
causa das respostas e entra em ``mensagens_processadas`` com o efeito.

``SolicitarPagamento`` nao tem evento de falha no contrato: orcamento ausente,
de outra ordem ou nao aprovado e recusa do provedor sao erro permanente (DLQ
com alerta, e o orquestrador compensa pelo prazo tecnico); provedor fora do ar
e transitorio (fila de retry, ADR-040).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.compartilhado.infraestrutura.mensageria.consumidor import Desfecho
from src.pagamento.aplicacao.use_cases import EstornarPagamento, SolicitarPagamento
from src.pagamento.infraestrutura.orcamentos import OrcamentosMongoAdapter
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import timedelta

    from src.compartilhado.dominio.relogio import Relogio
    from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
    from src.pagamento.aplicacao.ports import GatewayPagamento, MetricasDePagamento


def solicitar_pagamento(
    dados: Mapping[str, Any],
    uow: MongoUnitOfWork,
    *,
    gateway: GatewayPagamento,
    validade: timedelta,
    relogio: Relogio,
) -> Desfecho:
    """``PagamentoSolicitado``; repetido republica, depois da lapide descarta."""
    pagamento = SolicitarPagamento(
        uow,
        MongoPagamentoRepository(uow),
        OrcamentosMongoAdapter(uow),
        gateway,
        validade,
        relogio,
    ).executar(
        ordem_id=UUID(dados["ordem_id"]), orcamento_id=UUID(dados["orcamento_id"])
    )
    # A lapide nao tem cobranca: o comando atrasado sai sem efeito e sem resposta.
    if pagamento.checkout_url is None:
        return Desfecho.IGNORADA
    return Desfecho.PROCESSADA


def estornar_pagamento(
    dados: Mapping[str, Any],
    uow: MongoUnitOfWork,
    *,
    gateway: GatewayPagamento,
    metricas: MetricasDePagamento,
    relogio: Relogio,
) -> Desfecho:
    """Responde ``PagamentoCancelado``, ``PagamentoEstornado`` ou a falha do estorno."""
    EstornarPagamento(
        uow, MongoPagamentoRepository(uow), gateway, metricas, relogio
    ).executar(
        ordem_id=UUID(dados["ordem_id"]),
        pagamento_id=UUID(dados["pagamento_id"]) if "pagamento_id" in dados else None,
        motivo=dados["motivo"],
    )
    return Desfecho.PROCESSADA
