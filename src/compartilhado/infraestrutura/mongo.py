"""Cliente MongoDB e conversoes de tipos de dominio para BSON."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bson.decimal128 import Decimal128
from pymongo import MongoClient

from src.compartilhado.dominio.dinheiro import Dinheiro

if TYPE_CHECKING:
    from collections.abc import Mapping

type Documento = dict[str, Any]

# Falha rapido quando o banco cai: 5s em vez dos 30s padrao do driver.
_TIMEOUT_SELECAO_SERVIDOR_MS = 5000


def criar_cliente(uri: str) -> MongoClient[Documento]:
    """Cliente com UUID nativo (binario subtipo 4) e datas com timezone UTC."""
    return MongoClient(
        uri,
        uuidRepresentation="standard",
        tz_aware=True,
        serverSelectionTimeoutMS=_TIMEOUT_SELECAO_SERVIDOR_MS,
    )


def dinheiro_para_bson(dinheiro: Dinheiro) -> Documento:
    """Dinheiro persiste como Decimal128 (exato, nunca double) + moeda."""
    return {"valor": Decimal128(dinheiro.valor), "moeda": dinheiro.moeda}


def dinheiro_de_bson(documento: Mapping[str, Any]) -> Dinheiro:
    valor: Decimal128 = documento["valor"]
    return Dinheiro(valor=valor.to_decimal(), moeda=documento["moeda"])
