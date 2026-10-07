"""``k8s/base/replica-set.js``, o initContainer do Job de inicializacao: replica
set e usuarios no MongoDB com keyfile, como o StatefulSet o sobe."""

from __future__ import annotations

import pytest
from pymongo.errors import OperationFailure

from src.banco import preparar_banco
from src.compartilhado.infraestrutura.mongo import conferir_versao, criar_cliente
from src.seed import semear
from tests.integracao.apoio import MongoComChave

type Execucao = tuple[int, list[str]]


@pytest.fixture(scope="module")
def execucoes(mongo_com_chave: MongoComChave) -> tuple[Execucao, Execucao]:
    return mongo_com_chave.iniciar(), mongo_com_chave.iniciar()


def test_inicia_o_replica_set_e_cria_os_usuarios_uma_vez(
    execucoes: tuple[Execucao, Execucao],
) -> None:
    primeira, segunda = execucoes

    assert primeira == (
        0,
        [
            "replica set rs0 initiated with localhost:27017",
            "user billing created",
            "user exporter created",
        ],
    )
    assert segunda == (
        0,
        [
            "replica set rs0 already initiated",
            "user billing already exists",
            "user exporter already exists",
        ],
    )


def test_billing_prepara_o_banco_e_o_exporter_so_monitora(
    mongo_com_chave: MongoComChave, execucoes: tuple[Execucao, Execucao]
) -> None:
    with criar_cliente(mongo_com_chave.uri("billing", "billing")) as billing:
        banco = billing["billing"]
        preparar_banco(banco)
        # O Job roda a cada deploy: com as colecoes de pe, o validador entra por
        # collMod, que pede dbAdmin.
        preparar_banco(banco)
        conferir_versao(banco)
        assert semear(banco) == (11, 0)
        with pytest.raises(OperationFailure, match="not authorized"):
            billing.admin.command("replSetGetStatus")

    with criar_cliente(mongo_com_chave.uri("exporter", "admin")) as exporter:
        assert exporter.admin.command("replSetGetStatus")["ok"] == 1
        assert exporter["billing"].command("dbStats")["ok"] == 1
        with pytest.raises(OperationFailure, match="not authorized"):
            exporter["billing"]["precos_servicos"].find_one()


def test_senha_do_root_errada_reprova_o_job(mongo_com_chave: MongoComChave) -> None:
    status, saida = mongo_com_chave.iniciar(MONGO_INITDB_ROOT_PASSWORD="errada")

    assert status != 0
    assert any("Authentication failed" in linha for linha in saida)
