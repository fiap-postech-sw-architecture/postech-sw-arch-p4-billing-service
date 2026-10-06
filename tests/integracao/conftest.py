"""MongoDB real em replica set de um no (transacoes de verdade).

Um container por sessao (testcontainers) ou ``TEST_MONGODB_URI`` apontando
para um replica set ja existente; um banco novo por teste, apagado no fim.
"""

from __future__ import annotations

import os
import re
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from src.banco import preparar_banco
from src.compartilhado.infraestrutura.mongo import criar_cliente
from src.main import criar_app
from tests.integracao.apoio import RelogioFixo, configuracao

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fastapi import FastAPI
    from pymongo.database import Database


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integracao/" in str(item.path).replace(os.sep, "/"):
            item.add_marker(pytest.mark.integracao)


def _iniciar_replica_set(uri: str) -> None:
    with MongoClient(uri) as cliente:
        cliente.admin.command(
            "replSetInitiate",
            {"_id": "rs0", "members": [{"_id": 0, "host": "localhost:27017"}]},
        )
        prazo = time.monotonic() + 30
        while not cliente.admin.command("hello").get("isWritablePrimary"):
            if time.monotonic() > prazo:
                msg = "replica set nao elegeu primario em 30s"
                raise RuntimeError(msg)
            time.sleep(0.1)


@pytest.fixture(scope="session")
def mongo_uri() -> Iterator[str]:
    externo = os.environ.get("TEST_MONGODB_URI")
    if externo:
        yield externo
        return
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    container = (
        DockerContainer("mongo:7")
        .with_command(["--replSet", "rs0", "--bind_ip_all"])
        .with_exposed_ports(27017)
        .waiting_for(
            LogMessageWaitStrategy(re.compile(r"waiting for connections", re.I))
        )
    )
    with container:
        host = container.get_container_host_ip()
        porta = container.get_exposed_port(27017)
        uri = f"mongodb://{host}:{porta}/?directConnection=true"
        _iniciar_replica_set(uri)
        yield uri


@pytest.fixture(scope="session")
def cliente_mongo(mongo_uri: str) -> Iterator[MongoClient[dict[str, Any]]]:
    # Criado depois da eleicao: um cliente que viu o no antes do replica set
    # existir fica sem suporte a sessao ate o proximo heartbeat (10s).
    cliente = criar_cliente(mongo_uri)
    yield cliente
    cliente.close()


@pytest.fixture(scope="session")
def banco_da_sessao(
    cliente_mongo: MongoClient[dict[str, Any]],
) -> Iterator[Database[dict[str, Any]]]:
    # Um banco por sessao, esvaziado a cada teste: criar e apagar um banco por
    # teste esgota os descritores do mongod (o WiredTiger fecha arquivos de
    # colecao apagada so depois) e ele aborta com "Too many open files".
    nome = f"teste_{uuid4().hex}"
    banco = cliente_mongo[nome]
    preparar_banco(banco)
    yield banco
    cliente_mongo.drop_database(nome)


@pytest.fixture
def banco(
    banco_da_sessao: Database[dict[str, Any]],
) -> Database[dict[str, Any]]:
    for colecao in banco_da_sessao.list_collection_names():
        banco_da_sessao[colecao].delete_many({})
    return banco_da_sessao


@pytest.fixture
def relogio() -> RelogioFixo:
    return RelogioFixo()


@pytest.fixture
def app(banco: Database[dict[str, Any]], jwks_publicado: dict[str, Any]) -> FastAPI:
    return criar_app(configuracao(), banco=banco)


@pytest.fixture
def api(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as cliente:
        yield cliente


@pytest.fixture
def cabecalhos(emitir_token: Callable[..., str]) -> Callable[[str], dict[str, str]]:
    def cabecalhos(papel: str = "admin") -> dict[str, str]:
        return {"Authorization": f"Bearer {emitir_token(papel)}"}

    return cabecalhos
