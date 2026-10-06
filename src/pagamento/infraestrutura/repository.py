"""Repositorio MongoDB do pagamento: um documento por agregado."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pymongo.errors import DuplicateKeyError

from src.compartilhado.infraestrutura.mongo import (
    ESQUEMA_DINHEIRO,
    aplicar_validador,
    dinheiro_de_bson,
    dinheiro_para_bson,
)
from src.pagamento.dominio.cobranca import (
    Cobranca,
    EstornoAutomatico,
    NotificacaoRecebida,
)
from src.pagamento.dominio.estados import MotivoEstorno, StatusPagamento
from src.pagamento.dominio.exceptions import PagamentoJaSolicitadoError
from src.pagamento.dominio.pagamento import Pagamento

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from pymongo.database import Database

    from src.compartilhado.infraestrutura.mongo import Documento
    from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork

COLECAO = "pagamentos"


ESQUEMA: Documento = {
    "bsonType": "object",
    "required": ["ordem_id", "status", "criado_em", "recusas"],
    "properties": {
        "ordem_id": {"bsonType": "binData"},
        "status": {"enum": [status.value for status in StatusPagamento]},
        "criado_em": {"bsonType": "date"},
        "recusas": {"bsonType": ["int", "long"], "minimum": 0},
        "orcamento_id": {"bsonType": "binData"},
        "valor": ESQUEMA_DINHEIRO,
        "expira_em": {"bsonType": "date"},
        "referencia_pagamento": {"bsonType": ["string", "null"]},
        "notificacoes": {"bsonType": "array"},
        "estornos_automaticos": {"bsonType": "array"},
    },
}


def preparar_pagamentos(banco: Database[Documento]) -> None:
    aplicar_validador(banco, COLECAO, ESQUEMA)
    # Um pagamento (ou lapide) por ordem: a compensacao acha a cobranca por ele,
    # e o SolicitarPagamento atrasado encontra a lapide (RFC-004 secao 7.4).
    banco[COLECAO].create_index("ordem_id", unique=True)
    # Uma cobranca por orcamento; a lapide nao tem orcamento_id.
    banco[COLECAO].create_index(
        "orcamento_id",
        unique=True,
        partialFilterExpression={"orcamento_id": {"$exists": True}},
    )
    # Uma tentativa do provedor confirma no maximo um pagamento.
    banco[COLECAO].create_index(
        "referencia_pagamento",
        unique=True,
        partialFilterExpression={"referencia_pagamento": {"$type": "string"}},
    )
    banco[COLECAO].create_index([("status", 1), ("expira_em", 1)])


class MongoPagamentoRepository:
    def __init__(self, uow: MongoUnitOfWork) -> None:
        self._uow = uow
        self._colecao = uow.banco[COLECAO]

    def obter_por_id(self, pagamento_id: UUID) -> Pagamento | None:
        doc = self._colecao.find_one({"_id": pagamento_id}, session=self._uow.sessao)
        return _de_documento(doc) if doc else None

    def obter_por_ordem(self, ordem_id: UUID) -> Pagamento | None:
        doc = self._colecao.find_one({"ordem_id": ordem_id}, session=self._uow.sessao)
        return _de_documento(doc) if doc else None

    def listar_vencidos(self, agora: datetime, limite: int) -> list[UUID]:
        cursor = self._colecao.find(
            {"status": StatusPagamento.SOLICITADO.value, "expira_em": {"$lt": agora}},
            {"_id": 1},
            session=self._uow.sessao,
        )
        return [doc["_id"] for doc in cursor.sort("expira_em").limit(limite)]

    def listar_solicitados(self, limite: int) -> list[UUID]:
        cursor = self._colecao.find(
            {"status": StatusPagamento.SOLICITADO.value},
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
    cobranca = pagamento.cobranca
    documento: Documento = {
        "_id": pagamento.id,
        "ordem_id": pagamento.ordem_id,
        "status": pagamento.status.value,
        "criado_em": pagamento.criado_em,
        "recusas": pagamento.recusas,
        "referencia_pagamento": pagamento.referencia_pagamento,
        "confirmado_em": pagamento.confirmado_em,
        "encerrado_em": pagamento.encerrado_em,
        "motivo": pagamento.motivo,
        "estornado_em": pagamento.estornado_em,
        "motivo_estorno": (
            pagamento.motivo_estorno.value if pagamento.motivo_estorno else None
        ),
        "notificacoes": [
            {
                "recebida_em": n.recebida_em,
                "referencia_pagamento": n.referencia_pagamento,
                "status_provedor": n.status_provedor,
            }
            for n in pagamento.notificacoes
        ],
        "estornos_automaticos": [
            {
                "referencia_pagamento": e.referencia_pagamento,
                "registrado_em": e.registrado_em,
                "falha": e.falha,
            }
            for e in pagamento.estornos_automaticos
        ],
    }
    # A lapide nao tem cobranca: os campos ficam fora do documento.
    if cobranca is not None:
        documento |= {
            "orcamento_id": cobranca.orcamento_id,
            "valor": dinheiro_para_bson(cobranca.valor),
            "provedor": cobranca.provedor,
            "referencia_preferencia": cobranca.referencia_preferencia,
            "checkout_url": cobranca.checkout_url,
            "expira_em": cobranca.expira_em,
        }
    return documento


def _de_documento(doc: Documento) -> Pagamento:
    # Campos opcionais lidos com ``get``: documento gravado por versao anterior
    # (sem um campo novo) continua legivel (expand/contract).
    motivo_estorno = doc.get("motivo_estorno")
    return Pagamento.reconstituir(
        id=doc["_id"],
        ordem_id=doc["ordem_id"],
        criado_em=doc["criado_em"],
        cobranca=_cobranca(doc),
        status=StatusPagamento(doc["status"]),
        recusas=doc.get("recusas", 0),
        referencia_pagamento=doc.get("referencia_pagamento"),
        confirmado_em=doc.get("confirmado_em"),
        encerrado_em=doc.get("encerrado_em"),
        motivo=doc.get("motivo"),
        estornado_em=doc.get("estornado_em"),
        motivo_estorno=MotivoEstorno(motivo_estorno) if motivo_estorno else None,
        notificacoes=[
            NotificacaoRecebida(
                recebida_em=n["recebida_em"],
                referencia_pagamento=n["referencia_pagamento"],
                status_provedor=n["status_provedor"],
            )
            for n in doc.get("notificacoes", [])
        ],
        estornos_automaticos=[
            EstornoAutomatico(
                referencia_pagamento=e["referencia_pagamento"],
                registrado_em=e["registrado_em"],
                falha=e.get("falha"),
            )
            for e in doc.get("estornos_automaticos", [])
        ],
    )


def _cobranca(doc: Documento) -> Cobranca | None:
    if doc.get("checkout_url") is None:
        return None
    return Cobranca(
        orcamento_id=doc["orcamento_id"],
        valor=dinheiro_de_bson(doc["valor"]),
        provedor=doc["provedor"],
        referencia_preferencia=doc["referencia_preferencia"],
        checkout_url=doc["checkout_url"],
        expira_em=doc["expira_em"],
    )
