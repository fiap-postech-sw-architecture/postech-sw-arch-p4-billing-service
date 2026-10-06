"""Repositorio MongoDB do orcamento: um documento por agregado, linhas embutidas."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pymongo.errors import DuplicateKeyError

from src.compartilhado.infraestrutura.mongo import (
    ESQUEMA_DINHEIRO,
    aplicar_validador,
    dinheiro_de_bson,
    dinheiro_para_bson,
)
from src.orcamento.dominio.exceptions import OrcamentoJaGeradoError
from src.orcamento.dominio.orcamento import (
    CanalDecisao,
    Decisao,
    LinhaOrcamento,
    Orcamento,
    StatusOrcamento,
    TipoItem,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from pymongo.database import Database

    from src.compartilhado.infraestrutura.mongo import Documento
    from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork

COLECAO = "orcamentos"


ESQUEMA: Documento = {
    "bsonType": "object",
    "required": ["ordem_id", "status", "criado_em", "linhas"],
    "properties": {
        "ordem_id": {"bsonType": "binData"},
        "status": {"enum": [status.value for status in StatusOrcamento]},
        "criado_em": {"bsonType": "date"},
        "valido_ate": {"bsonType": ["date", "null"]},
        "linhas": {"bsonType": "array"},
        "total": ESQUEMA_DINHEIRO,
    },
}


def preparar_orcamentos(banco: Database[Documento]) -> None:
    aplicar_validador(banco, COLECAO, ESQUEMA)
    # Um orcamento (ou lapide) por ordem: a idempotencia do GerarOrcamento e a
    # compensacao por ordem dependem dele.
    banco[COLECAO].create_index("ordem_id", unique=True)
    banco[COLECAO].create_index([("status", 1), ("valido_ate", 1)])


class MongoOrcamentoRepository:
    def __init__(self, uow: MongoUnitOfWork) -> None:
        self._uow = uow
        self._colecao = uow.banco[COLECAO]

    def obter_por_id(self, orcamento_id: UUID) -> Orcamento | None:
        doc = self._colecao.find_one({"_id": orcamento_id}, session=self._uow.sessao)
        return _de_documento(doc) if doc else None

    def obter_por_ordem(self, ordem_id: UUID) -> Orcamento | None:
        doc = self._colecao.find_one({"ordem_id": ordem_id}, session=self._uow.sessao)
        return _de_documento(doc) if doc else None

    def listar_vencidos(self, agora: datetime, limite: int) -> list[UUID]:
        cursor = self._colecao.find(
            {"status": StatusOrcamento.PENDENTE.value, "valido_ate": {"$lt": agora}},
            {"_id": 1},
            session=self._uow.sessao,
        )
        return [doc["_id"] for doc in cursor.sort("valido_ate").limit(limite)]

    def salvar(self, orcamento: Orcamento) -> None:
        self._uow.registrar(orcamento)
        try:
            self._colecao.replace_one(
                {"_id": orcamento.id},
                _para_documento(orcamento),
                upsert=True,
                session=self._uow.sessao,
            )
        except DuplicateKeyError:
            raise OrcamentoJaGeradoError from None


def _para_documento(orcamento: Orcamento) -> Documento:
    decisao = orcamento.decisao
    return {
        "_id": orcamento.id,
        "ordem_id": orcamento.ordem_id,
        "status": orcamento.status.value,
        "linhas": [
            {
                "tipo": linha.tipo.value,
                "codigo": linha.codigo,
                "descricao": linha.descricao,
                "quantidade": linha.quantidade,
                "preco_unitario": dinheiro_para_bson(linha.preco_unitario),
            }
            for linha in orcamento.linhas
        ],
        # Total gravado para leitura humana/consulta; a fonte e a soma das linhas.
        "total": dinheiro_para_bson(orcamento.total),
        "criado_em": orcamento.criado_em,
        "valido_ate": orcamento.valido_ate,
        "decisao": (
            {
                "canal": decisao.canal.value,
                "decidido_em": decisao.decidido_em,
                "decidido_por": decisao.decidido_por,
            }
            if decisao
            else None
        ),
        "motivo_cancelamento": orcamento.motivo_cancelamento,
    }


def _de_documento(doc: Documento) -> Orcamento:
    # Campos opcionais lidos com ``get``: documento gravado por versao anterior
    # (sem um campo novo) continua legivel (expand/contract).
    decisao = doc.get("decisao")
    return Orcamento.reconstituir(
        id=doc["_id"],
        ordem_id=doc["ordem_id"],
        linhas=tuple(
            LinhaOrcamento(
                tipo=TipoItem(linha["tipo"]),
                codigo=linha["codigo"],
                descricao=linha["descricao"],
                quantidade=linha["quantidade"],
                preco_unitario=dinheiro_de_bson(linha["preco_unitario"]),
            )
            for linha in doc.get("linhas", [])
        ),
        criado_em=doc["criado_em"],
        valido_ate=doc.get("valido_ate"),
        status=StatusOrcamento(doc["status"]),
        decisao=(
            Decisao(
                canal=CanalDecisao(decisao["canal"]),
                decidido_em=decisao["decidido_em"],
                decidido_por=decisao.get("decidido_por"),
            )
            if decisao
            else None
        ),
        motivo_cancelamento=doc.get("motivo_cancelamento"),
    )
