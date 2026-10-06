"""Repositorio MongoDB do pagamento: um documento por agregado."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pymongo.errors import DuplicateKeyError

from src.compartilhado.infraestrutura.mongo import (
    dinheiro_de_bson,
    dinheiro_para_bson,
)
from src.pagamento.dominio.exceptions import PagamentoJaSolicitadoError
from src.pagamento.dominio.pagamento import (
    NotificacaoRecebida,
    Pagamento,
    StatusPagamento,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from pymongo.database import Database

    from src.compartilhado.infraestrutura.mongo import Documento
    from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork

COLECAO = "pagamentos"


def criar_indices_pagamentos(banco: Database[Documento]) -> None:
    # Um pagamento por orcamento: a idempotencia do SolicitarPagamento depende dele.
    banco[COLECAO].create_index("orcamento_id", unique=True)
    banco[COLECAO].create_index([("status", 1), ("expira_em", 1)])


class MongoPagamentoRepository:
    def __init__(self, uow: MongoUnitOfWork) -> None:
        self._uow = uow
        self._colecao = uow.banco[COLECAO]

    def obter_por_id(self, pagamento_id: UUID) -> Pagamento | None:
        doc = self._colecao.find_one({"_id": pagamento_id}, session=self._uow.sessao)
        return _de_documento(doc) if doc else None

    def obter_por_orcamento(self, orcamento_id: UUID) -> Pagamento | None:
        doc = self._colecao.find_one(
            {"orcamento_id": orcamento_id}, session=self._uow.sessao
        )
        return _de_documento(doc) if doc else None

    def listar_vencidos(self, agora: datetime, limite: int) -> list[UUID]:
        cursor = self._colecao.find(
            {"status": StatusPagamento.PENDENTE.value, "expira_em": {"$lt": agora}},
            {"_id": 1},
            session=self._uow.sessao,
        )
        return [doc["_id"] for doc in cursor.sort("expira_em").limit(limite)]

    def salvar(self, pagamento: Pagamento) -> None:
        self._uow.registrar(pagamento)
        try:
            self._colecao.replace_one(
                {"_id": pagamento.id},
                _para_documento(pagamento),
                upsert=True,
                session=self._uow.sessao,
            )
        except DuplicateKeyError:
            raise PagamentoJaSolicitadoError from None


def _para_documento(pagamento: Pagamento) -> Documento:
    return {
        "_id": pagamento.id,
        "ordem_id": pagamento.ordem_id,
        "orcamento_id": pagamento.orcamento_id,
        "valor": dinheiro_para_bson(pagamento.valor),
        "status": pagamento.status.value,
        "provedor": pagamento.provedor,
        "referencia_preferencia": pagamento.referencia_preferencia,
        "referencia_pagamento": pagamento.referencia_pagamento,
        "checkout_url": pagamento.checkout_url,
        "criado_em": pagamento.criado_em,
        "expira_em": pagamento.expira_em,
        "confirmado_em": pagamento.confirmado_em,
        "estornado_em": pagamento.estornado_em,
        "chave_estorno": pagamento.chave_estorno,
        "motivo": pagamento.motivo,
        "notificacoes": [
            {
                "recebida_em": n.recebida_em,
                "referencia_pagamento": n.referencia_pagamento,
                "status_provedor": n.status_provedor,
            }
            for n in pagamento.notificacoes
        ],
    }


def _de_documento(doc: dict[str, Any]) -> Pagamento:
    return Pagamento(
        id=doc["_id"],
        _ordem_id=doc["ordem_id"],
        _orcamento_id=doc["orcamento_id"],
        _valor=dinheiro_de_bson(doc["valor"]),
        _status=StatusPagamento(doc["status"]),
        _provedor=doc["provedor"],
        _referencia_preferencia=doc["referencia_preferencia"],
        _referencia_pagamento=doc["referencia_pagamento"],
        _checkout_url=doc["checkout_url"],
        _criado_em=doc["criado_em"],
        _expira_em=doc["expira_em"],
        _confirmado_em=doc["confirmado_em"],
        _estornado_em=doc["estornado_em"],
        _chave_estorno=doc["chave_estorno"],
        _motivo=doc["motivo"],
        _notificacoes=[
            NotificacaoRecebida(
                recebida_em=n["recebida_em"],
                referencia_pagamento=n["referencia_pagamento"],
                status_provedor=n["status_provedor"],
            )
            for n in doc["notificacoes"]
        ],
    )
