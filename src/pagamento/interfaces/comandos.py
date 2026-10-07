"""Comandos da saga do pagamento, chamados pelo consumidor de ``billing.comandos``.

Traduzem o ``dados`` do contrato (ja validado) para os casos de uso, como os
routers fazem com o HTTP. A unidade de trabalho e a da mensagem: o handler
grava o efeito sem comitar, e o consumidor comita junto a outbox (o comando e
a causa das respostas) e ``mensagens_processadas``.

``SolicitarPagamento`` nao tem evento de falha no contrato. Orcamento que nao
esta aprovado e descompasso de estado: o comando e ignorado (ack, log
``command_ignored`` com o codigo, sem resposta), e a saga segue pelo evento que
ja recebeu ou pelo prazo. Orcamento ausente ou de outra ordem e recusa do
provedor sao falha permanente (DLQ com alerta, e o orquestrador compensa pelo
prazo tecnico); provedor fora do ar e transitorio (fila de retry, ADR-040).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.compartilhado.infraestrutura.mensageria.consumidor import Desfecho
from src.pagamento.aplicacao.use_cases import EstornarPagamento, SolicitarPagamento
from src.pagamento.dominio.exceptions import OrcamentoNaoAprovadoError
from src.pagamento.infraestrutura.orcamentos import OrcamentosMongoAdapter
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import timedelta

    from src.compartilhado.dominio.relogio import Relogio
    from src.compartilhado.infraestrutura.unit_of_work import UnidadeDaMensagem
    from src.pagamento.aplicacao.ports import GatewayPagamento, MetricasDePagamento

_log = logging.getLogger(__name__)


def solicitar_pagamento(
    dados: Mapping[str, Any],
    uow: UnidadeDaMensagem,
    *,
    gateway: GatewayPagamento,
    validade: timedelta,
    relogio: Relogio,
) -> Desfecho:
    """``PagamentoSolicitado``; repetido republica, depois da lapide descarta."""
    ordem_id = UUID(dados["ordem_id"])
    try:
        pagamento = SolicitarPagamento(
            uow,
            MongoPagamentoRepository(uow),
            OrcamentosMongoAdapter(uow),
            gateway,
            validade,
            relogio,
        ).executar(ordem_id=ordem_id, orcamento_id=UUID(dados["orcamento_id"]))
    except OrcamentoNaoAprovadoError as exc:
        _log.info(
            "command_ignored",
            extra={
                "comando": "SolicitarPagamento",
                "motivo": exc.codigo,
                "ordem_id": str(ordem_id),
            },
        )
        return Desfecho.IGNORADA
    # Depois da lapide o comando atrasado sai sem efeito e sem resposta.
    if pagamento.lapide:
        return Desfecho.IGNORADA
    return Desfecho.PROCESSADA


def estornar_pagamento(
    dados: Mapping[str, Any],
    uow: UnidadeDaMensagem,
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
