"""Repositorios MongoDB da tabela de precos."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pymongo.errors import DuplicateKeyError

from src.compartilhado.infraestrutura.mongo import (
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


def criar_indices_precos(banco: Database[Documento]) -> None:
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


def _servico(doc: dict[str, Any]) -> PrecoServico:
    return PrecoServico(
        id=doc["_id"],
        _codigo=doc["codigo"],
        _nome=doc["nome"],
        _descricao=doc["descricao"],
        _preco=dinheiro_de_bson(doc["preco"]),
        _ativo=doc["ativo"],
    )


def _peca(doc: dict[str, Any]) -> PrecoPeca:
    return PrecoPeca(
        id=doc["_id"],
        _sku=doc["sku"],
        _nome=doc["nome"],
        _preco=dinheiro_de_bson(doc["preco"]),
        _ativo=doc["ativo"],
    )
