"""MongoDB real em replica set de um no (transacoes de verdade).

Um container por sessao (testcontainers) ou ``TEST_MONGODB_URI`` apontando
para um replica set ja existente; um banco novo por teste, apagado no fim.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from src.banco import preparar_banco
from src.compartilhado.infraestrutura.mongo import criar_cliente
from src.main import criar_app
from tests.integracao.apoio import (
    FILAS_DO_BILLING,
    SENHA_DO_ADMIN,
    BrokerDeTeste,
    RelogioFixo,
    configuracao,
    definicoes_de_teste,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fastapi import FastAPI
    from pymongo.database import Database


# Mesma versao do docker-compose.yml (e do compose da plataforma).
IMAGEM_MONGO = "mongo:7.0.43"
IMAGEM_RABBITMQ = "rabbitmq:4.3.6-management"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integracao/" in str(item.path).replace(os.sep, "/"):
            item.add_marker(pytest.mark.integracao)


# Colima (macOS): o testcontainers usa o SDK do Docker, que le DOCKER_HOST e
# nao os contexts do CLI; o Ryuk precisa do socket visto de dentro da VM.
# Inocuo no CI (Linux com /var/run/docker.sock) e com Docker Desktop.
_SOCKET_COLIMA = Path.home() / ".colima" / "default" / "docker.sock"


def _docker_do_colima(ambiente: pytest.MonkeyPatch) -> None:
    if not _SOCKET_COLIMA.exists():
        return
    if "DOCKER_HOST" not in os.environ:
        ambiente.setenv("DOCKER_HOST", f"unix://{_SOCKET_COLIMA}")
    if "TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE" not in os.environ:
        ambiente.setenv("TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE", "/var/run/docker.sock")


@pytest.fixture(scope="session")
def mongo_uri() -> Iterator[str]:
    externo = os.environ.get("TEST_MONGODB_URI")
    if externo:
        yield externo
        return
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    with pytest.MonkeyPatch.context() as ambiente:
        _docker_do_colima(ambiente)
        container = (
            DockerContainer(IMAGEM_MONGO)
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
            with criar_cliente(uri) as cliente:
                cliente.admin.command(
                    "replSetInitiate",
                    {"_id": "rs0", "members": [{"_id": 0, "host": "localhost:27017"}]},
                )
            # O no se elege primario sozinho: espera a linha do log, sem sondar.
            LogMessageWaitStrategy("Transition to primary complete").wait_until_ready(
                container
            )
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


@pytest.fixture(scope="session")
def _broker_da_sessao() -> Iterator[BrokerDeTeste]:
    """RabbitMQ 4.3.6 com a topologia do platform (copiada em ``rabbitmq/``),
    carregada pela API de gerenciamento com o TTL de retry de 100 ms."""
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    with pytest.MonkeyPatch.context() as ambiente:
        _docker_do_colima(ambiente)
        container = (
            DockerContainer(IMAGEM_RABBITMQ)
            .with_env("RABBITMQ_DEFAULT_USER", "admin")
            .with_env("RABBITMQ_DEFAULT_PASS", SENHA_DO_ADMIN)
            .with_exposed_ports(5672, 15672)
            .waiting_for(LogMessageWaitStrategy("Server startup complete"))
        )
        with container:
            # 127.0.0.1 e nao "localhost": o pika tentaria o ::1 antes, em vao.
            host = container.get_container_host_ip().replace("localhost", "127.0.0.1")
            api = f"http://{host}:{container.get_exposed_port(15672)}/api"
            with httpx.Client(auth=("admin", SENHA_DO_ADMIN), timeout=10) as http:
                resposta = http.post(f"{api}/definitions", json=definicoes_de_teste())
                resposta.raise_for_status()
            yield BrokerDeTeste(
                host=host,
                porta=int(container.get_exposed_port(5672)),
                api=api,
                container=container,
            )


@pytest.fixture
def broker(_broker_da_sessao: BrokerDeTeste) -> BrokerDeTeste:
    """Filas do Billing e a do OS vazias a cada teste (isolamento)."""
    with _broker_da_sessao.conectar() as conexao:
        canal = conexao.channel()
        for fila in FILAS_DO_BILLING:
            canal.queue_purge(fila)
    return _broker_da_sessao
