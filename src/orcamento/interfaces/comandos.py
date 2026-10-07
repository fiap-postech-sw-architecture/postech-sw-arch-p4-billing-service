"""Comandos da saga do orcamento, chamados pelo consumidor de ``billing.comandos``.

Traduzem o ``dados`` do contrato (ja validado) para os casos de uso, como os
routers fazem com o HTTP. A unidade de trabalho e a da mensagem: o comando e a
causa das respostas e entra em ``mensagens_processadas`` com o efeito.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.compartilhado.infraestrutura.mensageria.consumidor import Desfecho
from src.orcamento.aplicacao.dtos import ItemSolicitado
from src.orcamento.aplicacao.use_cases import CancelarOrcamento, GerarOrcamento
from src.orcamento.dominio.orcamento import TipoItem
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.orcamento.infraestrutura.tabela_de_precos import TabelaDePrecosMongoAdapter

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import timedelta

    from src.compartilhado.dominio.relogio import Relogio
    from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
    from src.orcamento.aplicacao.link_decisao import LinkDeDecisao


def gerar_orcamento(
    dados: Mapping[str, Any],
    uow: MongoUnitOfWork,
    *,
    link: LinkDeDecisao,
    validade: timedelta,
    relogio: Relogio,
) -> Desfecho:
    """``OrcamentoGerado`` ou ``GeracaoDeOrcamentoFalhou``; repetido republica."""
    orcamento = GerarOrcamento(
        uow,
        MongoOrcamentoRepository(uow),
        TabelaDePrecosMongoAdapter(uow),
        link,
        validade,
        relogio,
    ).executar(
        ordem_id=UUID(dados["ordem_id"]),
        itens=[
            ItemSolicitado(
                tipo=TipoItem(item["tipo"]),
                codigo=item["codigo"],
                quantidade=item["quantidade"],
            )
            for item in dados["itens"]
        ],
    )
    # A lapide (cancelamento que chegou antes) descarta o comando, sem resposta.
    if orcamento is not None and orcamento.valido_ate is None:
        return Desfecho.IGNORADA
    return Desfecho.PROCESSADA


def cancelar_orcamento(
    dados: Mapping[str, Any], uow: MongoUnitOfWork, *, relogio: Relogio
) -> Desfecho:
    """``OrcamentoCancelado`` sempre (lapide se o ``GerarOrcamento`` nao chegou)."""
    CancelarOrcamento(uow, MongoOrcamentoRepository(uow), relogio).executar(
        ordem_id=UUID(dados["ordem_id"]),
        orcamento_id=UUID(dados["orcamento_id"]) if "orcamento_id" in dados else None,
        motivo=dados["motivo"],
    )
    return Desfecho.PROCESSADA
