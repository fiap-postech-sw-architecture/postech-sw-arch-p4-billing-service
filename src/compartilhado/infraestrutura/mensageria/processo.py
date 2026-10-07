"""Boot comum do relay e do consumidor (``python -m src.relay|src.consumidor``).

Instala a telemetria e os sinais, serve o ``/metrics``, abre o cliente do
MongoDB (conferindo a versao do banco) e prepara a conexao AMQP; no fim fecha
tudo na ordem inversa, inclusive quando o boot falha no meio.
"""

from __future__ import annotations

import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from prometheus_client import start_http_server

from src.compartilhado.infraestrutura.mensageria.amqp import CanalAmqp, parametros
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    configurar_telemetria,
)
from src.compartilhado.infraestrutura.mongo import conferir_versao, criar_cliente
from src.compartilhado.infraestrutura.processo import instalar_sinais

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from pymongo.database import Database

    from src.compartilhado.infraestrutura.mongo import Documento
    from src.configuracao import ConfiguracaoDoConsumidor, ConfiguracaoDoRelay


@dataclass(frozen=True, slots=True)
class Processo:
    """O que o laco de cada processo usa: banco, canal e o sinal de parada."""

    banco: Database[Documento]
    canal: CanalAmqp
    parar: threading.Event


@contextmanager
def processo_da_mensageria(
    nome: str,
    config: ConfiguracaoDoRelay | ConfiguracaoDoConsumidor,
    parar: threading.Event | None,
    *,
    filas: Iterable[str] = (),
    exchanges: Iterable[str] = (),
) -> Iterator[Processo]:
    """Recursos do processo ``nome``; ``parar`` vem pronto so nos testes (sem
    ele, o SIGTERM o aciona)."""
    with ExitStack() as fechar:
        fechar.callback(configurar_telemetria(nome).shutdown)
        if parar is None:
            parar = threading.Event()
            instalar_sinais(parar)
        servidor, _ = start_http_server(config.porta_metricas)
        fechar.callback(servidor.shutdown)
        cliente = criar_cliente(config.banco.mongodb_uri)
        fechar.callback(cliente.close)
        canal = CanalAmqp(
            parametros(config.rabbitmq_url, nome=f"billing-{nome}"),
            filas=filas,
            exchanges=exchanges,
        )
        fechar.callback(canal.fechar)
        banco = cliente[config.banco.mongodb_banco]
        conferir_versao(banco)
        yield Processo(banco, canal, parar)
