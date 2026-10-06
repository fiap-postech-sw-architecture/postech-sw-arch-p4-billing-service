"""Repositorios MongoDB da tabela de precos."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pymongo.errors import DuplicateKeyError

from src.compartilhado.infraestrutura.mongo import (
    ESQUEMA_DINHEIRO,
    aplicar_validador,
    dinheiro_de_bson,
    dinheiro_para_bson,
)
from src.precos.dominio.exceptions import PrecoJaCadastradoError
from src.precos.dominio.preco import PrecoPeca, PrecoServico

if TYPE_CHECKING:
    from collections.abc import Collection

    from pymongo.database import Database

    from src.compartilhado.infraestrutura.mongo import Documento
    from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork

COLECAO_SERVICOS = "precos_servicos"
COLECAO_PECAS = "precos_pecas"


_CODIGO = {"bsonType": "string", "minLength": 1, "maxLength": 50}
_TEXTO = {"bsonType": "string", "minLength": 1}
ESQUEMA_SERVICOS: Documento = {
    "bsonType": "object",
    "required": ["codigo", "nome", "descricao", "preco", "ativo"],
    "properties": {
        "codigo": _CODIGO,
        "nome": _TEXTO,
        "descricao": _TEXTO,
        "preco": ESQUEMA_DINHEIRO,
        "ativo": {"bsonType": "bool"},
    },
}
ESQUEMA_PECAS: Documento = {
    "bsonType": "object",
    "required": ["sku", "nome", "preco", "ativo"],
    "properties": {
        "sku": _CODIGO,
        "nome": _TEXTO,
        "preco": ESQUEMA_DINHEIRO,
        "ativo": {"bsonType": "bool"},
    },
}


def preparar_precos(banco: Database[Documento]) -> None:
    aplicar_validador(banco, COLECAO_SERVICOS, ESQUEMA_SERVICOS)
    aplicar_validador(banco, COLECAO_PECAS, ESQUEMA_PECAS)
    banco[COLECAO_SERVICOS].create_index("codigo", unique=True)
    banco[COLECAO_PECAS].create_index("sku", unique=True)


class MongoPrecoServicoRepository:
    def __init__(self, uow: MongoUnitOfWork) -> None:
        self._uow = uow
        self._colecao = uow.banco[COLECAO_SERVICOS]

    def obter_por_codigo(self, codigo: str) -> PrecoServico | None:
        doc = self._colecao.find_one({"codigo": codigo}, session=self._uow.sessao)
        return _servico(doc) if doc else None

    def obter_por_codigos(self, codigos: Collection[str]) -> dict[str, PrecoServico]:
        cursor = self._colecao.find(
            {"codigo": {"$in": list(codigos)}}, session=self._uow.sessao
        )
        return {doc["codigo"]: _servico(doc) for doc in cursor}

    def listar(self, *, offset: int, limit: int) -> list[PrecoServico]:
        cursor = self._colecao.find({}, session=self._uow.sessao)
        return [
            _servico(doc) for doc in cursor.sort("codigo").skip(offset).limit(limit)
        ]

    def contar(self) -> int:
        return self._colecao.count_documents({}, session=self._uow.sessao)

    def salvar(self, preco: PrecoServico) -> None:
        self._uow.registrar(preco)
        documento = {
            "_id": preco.id,
            "codigo": preco.codigo,
            "nome": preco.nome,
            "descricao": preco.descricao,
            "preco": dinheiro_para_bson(preco.preco),
            "ativo": preco.ativo,
        }
        try:
            self._colecao.replace_one(
                {"_id": preco.id}, documento, upsert=True, session=self._uow.sessao
            )
        except DuplicateKeyError:
            msg = f"Servico {preco.codigo} ja cadastrado na tabela de precos"
            raise PrecoJaCadastradoError(msg) from None


class MongoPrecoPecaRepository:
    def __init__(self, uow: MongoUnitOfWork) -> None:
        self._uow = uow
        self._colecao = uow.banco[COLECAO_PECAS]

    def obter_por_codigo(self, sku: str) -> PrecoPeca | None:
        doc = self._colecao.find_one({"sku": sku}, session=self._uow.sessao)
        return _peca(doc) if doc else None

    def obter_por_codigos(self, skus: Collection[str]) -> dict[str, PrecoPeca]:
        cursor = self._colecao.find(
            {"sku": {"$in": list(skus)}}, session=self._uow.sessao
        )
        return {doc["sku"]: _peca(doc) for doc in cursor}

    def listar(self, *, offset: int, limit: int) -> list[PrecoPeca]:
        cursor = self._colecao.find({}, session=self._uow.sessao)
        return [_peca(doc) for doc in cursor.sort("sku").skip(offset).limit(limit)]

    def contar(self) -> int:
        return self._colecao.count_documents({}, session=self._uow.sessao)

    def salvar(self, preco: PrecoPeca) -> None:
        self._uow.registrar(preco)
        documento = {
            "_id": preco.id,
            "sku": preco.sku,
            "nome": preco.nome,
            "preco": dinheiro_para_bson(preco.preco),
            "ativo": preco.ativo,
        }
        try:
            self._colecao.replace_one(
                {"_id": preco.id}, documento, upsert=True, session=self._uow.sessao
            )
        except DuplicateKeyError:
            msg = f"Peca {preco.sku} ja cadastrada na tabela de precos"
            raise PrecoJaCadastradoError(msg) from None


def _servico(doc: Documento) -> PrecoServico:
    return PrecoServico.reconstituir(
        id=doc["_id"],
        codigo=doc["codigo"],
        nome=doc["nome"],
        descricao=doc["descricao"],
        preco=dinheiro_de_bson(doc["preco"]),
        ativo=doc["ativo"],
    )


def _peca(doc: Documento) -> PrecoPeca:
    return PrecoPeca.reconstituir(
        id=doc["_id"],
        sku=doc["sku"],
        nome=doc["nome"],
        preco=dinheiro_de_bson(doc["preco"]),
        ativo=doc["ativo"],
    )
