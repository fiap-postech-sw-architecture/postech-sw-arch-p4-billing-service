"""Processo ``relay``: publica os eventos da outbox (``python -m src.relay``).

Sem conexao com o broker o relay nao reivindica linhas: reconecta com backoff
dobrado ate 30 s (ADR-036). Conectado, entrega em lotes e, com a outbox vazia,
espera atendendo o broker (heartbeat e fechamento). O arquivo de vida e tocado
a cada volta, com ``pronto`` so enquanto conectado (liveness e readiness, RFC-004
secao 6); SIGTERM conclui a mensagem em curso e fecha as conexoes.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Final

from prometheus_client import REGISTRY, start_http_server
from pymongo.errors import PyMongoError

from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.mensageria.amqp import (
    ESPERA_MAXIMA_SEGUNDOS,
    CanalAmqp,
    manter_conectado,
    parametros,
)
from src.compartilhado.infraestrutura.mensageria.contratos import EXCHANGE_EVENTOS
from src.compartilhado.infraestrutura.mensageria.metricas import ColetorDaOutbox
from src.compartilhado.infraestrutura.mensageria.relay import RelayDaOutbox
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    configurar_telemetria,
)
from src.compartilhado.infraestrutura.mongo import conferir_versao, criar_cliente
from src.compartilhado.infraestrutura.processo import instalar_sinais, sinalizar
from src.compartilhado.infraestrutura.unit_of_work import COLECAO_OUTBOX
from src.configuracao import ConfiguracaoDoRelay

if TYPE_CHECKING:
    from pathlib import Path

_log = logging.getLogger(__name__)

LOTE: Final = 50
INTERVALO_SEGUNDOS: Final = 1.0


def rodar(
    relay: RelayDaOutbox,
    canal: CanalAmqp,
    *,
    parar: threading.Event,
    heartbeat: Path,
    intervalo: float = INTERVALO_SEGUNDOS,
    espera_maxima: float = ESPERA_MAXIMA_SEGUNDOS,
) -> None:
    """Laco do processo: conecta, entrega enquanto conectado, reconecta."""
    manter_conectado(
        canal,
        lambda: _entregar_enquanto_conectado(relay, canal, parar, heartbeat, intervalo),
        parar=parar,
        heartbeat=heartbeat,
        processo="relay",
        espera_maxima=espera_maxima,
    )


def _entregar_enquanto_conectado(
    relay: RelayDaOutbox,
    canal: CanalAmqp,
    parar: threading.Event,
    heartbeat: Path,
    intervalo: float,
) -> None:
    while not parar.is_set():
        sinalizar(heartbeat, pronto=True)
        try:
            lote_cheio = relay.entregar_pendentes(LOTE) == LOTE
        except PyMongoError:
            # Banco fora: reiniciar o pod nao o conserta; tenta de novo.
            _log.exception("relay_database_unavailable")
            lote_cheio = False
        if not lote_cheio:
            canal.aguardar(intervalo)


def main(parar: threading.Event | None = None) -> None:
    configurar_logging()
    config = ConfiguracaoDoRelay.do_ambiente()
    provedor = configurar_telemetria("relay")
    if parar is None:
        parar = threading.Event()
        instalar_sinais(parar)
    servidor, _ = start_http_server(config.porta_metricas)
    cliente = criar_cliente(config.banco.mongodb_uri)
    canal = CanalAmqp(
        parametros(config.rabbitmq_url, nome="billing-relay"),
        exchanges=(EXCHANGE_EVENTOS,),
    )
    banco = cliente[config.banco.mongodb_banco]
    coletor = ColetorDaOutbox(COLECAO_OUTBOX, banco)
    REGISTRY.register(coletor)
    try:
        conferir_versao(banco)
        _log.info("relay_started")
        rodar(
            RelayDaOutbox(banco, canal, usuario=config.rabbitmq_usuario),
            canal,
            parar=parar,
            heartbeat=config.heartbeat,
        )
        _log.info("relay_stopped")
    finally:
        REGISTRY.unregister(coletor)
        canal.fechar()
        cliente.close()
        servidor.shutdown()
        provedor.shutdown()


if __name__ == "__main__":  # pragma: no cover
    main()
