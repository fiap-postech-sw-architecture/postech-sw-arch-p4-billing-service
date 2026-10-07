"""Cliente MongoDB, conversoes de tipos de dominio para BSON e preparacao.

A preparacao do banco (indices, validadores ``$jsonSchema`` e versao) roda
uma vez, antes da API e do ``prazos`` (``python -m src.banco``); eles so
conferem a versao (RFC-004 secao 7.4, ADR-037).
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any, Final

import pymongo
from bson.decimal128 import Decimal128
from pymongo import MongoClient
from pymongo.errors import CollectionInvalid

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.compartilhado.dominio.exceptions import ValorInvalidoError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pymongo.database import Database

type Documento = dict[str, Any]  # documento BSON cru (o driver o tipa assim)

# Falha rapido quando o banco cai: 5 s para achar o primario em vez dos 30 s
# padrao, e no maximo 10 s por operacao (CSOT): um mongod travado no meio da
# operacao nao prende a thread para sempre nem esgota o threadpool da API. O
# laco de repeticao da transacao tem teto proprio (unit_of_work.py): o
# with_transaction repete por ate 120 s sem olhar este limite.
_TIMEOUT_SELECAO_SERVIDOR_MS: Final = 5000
_TIMEOUT_OPERACAO_MS: Final = 10_000
# Threadpool da API (40) com folga; o processo prazos usa uma conexao.
_MAX_CONEXOES: Final = 50

COLECAO_VERSAO: Final = "versao_do_banco"
# 2: outbox com exchange, routing key e indice de claim; mensagens_processadas.
VERSAO_DO_BANCO: Final = 2

# Dinheiro como subdocumento {valor: Decimal128, moeda} (nunca double).
ESQUEMA_DINHEIRO: Final[Documento] = {
    "bsonType": "object",
    "required": ["valor", "moeda"],
    "properties": {
        "valor": {"bsonType": "decimal"},
        "moeda": {"bsonType": "string", "minLength": 3, "maxLength": 3},
    },
}


class BancoNaoPreparadoError(RuntimeError):
    """Indices e validadores ausentes ou de versao antiga: rode o init."""


class DocumentoInvalidoError(RuntimeError):
    """Documento gravado que fere as invariantes do agregado: defeito de dado
    (500 com o traceback no log), nunca o 422 de entrada do chamador."""


def reidratacao[T](ler: Callable[[Documento], T]) -> Callable[[Documento], T]:
    """Leitura de documento: invariante violada vira ``DocumentoInvalidoError``."""

    @functools.wraps(ler)
    def reidratar(doc: Documento) -> T:
        try:
            return ler(doc)
        except ValorInvalidoError as exc:
            msg = f"Documento {doc.get('_id')} fora das invariantes do agregado"
            raise DocumentoInvalidoError(msg) from exc

    return reidratar


def criar_cliente(uri: str) -> MongoClient[Documento]:
    """Cliente com UUID nativo (binario subtipo 4), datas em UTC e limites."""
    return MongoClient(
        uri,
        uuidRepresentation="standard",
        tz_aware=True,
        serverSelectionTimeoutMS=_TIMEOUT_SELECAO_SERVIDOR_MS,
        timeoutMS=_TIMEOUT_OPERACAO_MS,
        maxPoolSize=_MAX_CONEXOES,
    )


def dinheiro_para_bson(dinheiro: Dinheiro) -> Documento:
    """Dinheiro persiste como Decimal128 (exato, nunca double) + moeda."""
    return {"valor": Decimal128(dinheiro.valor), "moeda": dinheiro.moeda}


def dinheiro_de_bson(documento: Mapping[str, Any]) -> Dinheiro:
    valor: Decimal128 = documento["valor"]
    return Dinheiro(valor=valor.to_decimal(), moeda=documento["moeda"])


def aplicar_validador(
    banco: Database[Documento], colecao: str, esquema: Documento
) -> None:
    """Cria a colecao com o ``$jsonSchema`` ou o atualiza (idempotente).

    Nivel ``moderate``: valida insercoes e atualizacoes de documentos validos;
    documento antigo fora do esquema ainda pode ser corrigido (expand/contract).
    """
    opcoes: Documento = {
        "validator": {"$jsonSchema": esquema},
        "validationLevel": "moderate",
    }
    try:
        banco.create_collection(colecao, **opcoes)
    except CollectionInvalid:  # ja existe: so troca o validador
        banco.command("collMod", colecao, **opcoes)


def marcar_versao(banco: Database[Documento]) -> None:
    banco[COLECAO_VERSAO].update_one(
        {"_id": "billing"}, {"$max": {"versao": VERSAO_DO_BANCO}}, upsert=True
    )


def conferir_versao(banco: Database[Documento]) -> None:
    """Levanta ``BancoNaoPreparadoError`` se o init nao rodou nesta versao."""
    marca = banco[COLECAO_VERSAO].find_one({"_id": "billing"})
    if marca is None or marca.get("versao", 0) < VERSAO_DO_BANCO:
        msg = "Banco sem indices e validadores desta versao: rode python -m src.banco"
        raise BancoNaoPreparadoError(msg)


def verificar_prontidao(banco: Database[Documento], *, segundos: float = 2.0) -> None:
    """Ping e versao do banco em no maximo ``segundos`` (readiness)."""
    with pymongo.timeout(segundos):
        banco.command("ping")
        conferir_versao(banco)
