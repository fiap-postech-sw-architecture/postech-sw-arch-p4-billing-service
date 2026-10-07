"""Processo ``relay``: publica os eventos da outbox (``python -m src.relay``).

Sem conexao com o broker o relay nao reivindica linhas: reconecta com backoff
dobrado ate 30 s (ADR-036). Conectado, entrega em lotes e, com a outbox vazia,
espera atendendo o broker (heartbeat e fechamento). O arquivo de vida e tocado
a cada volta, com ``pronto`` so enquanto conectado (liveness e readiness, RFC-004
secao 6); SIGTERM conclui a mensagem em curso e fecha as conexoes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from prometheus_client import REGISTRY
from pymongo.errors import PyMongoError

from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.mensageria.amqp import (
    ESPERA_MAXIMA_SEGUNDOS,
    CanalAmqp,
    manter_conectado,
)
from src.compartilhado.infraestrutura.mensageria.contratos import EXCHANGE_EVENTOS
from src.compartilhado.infraestrutura.mensageria.metricas import ColetorDaOutbox
from src.compartilhado.infraestrutura.mensageria.processo import (
    processo_da_mensageria,
)
from src.compartilhado.infraestrutura.mensageria.relay import RelayDaOutbox
from src.compartilhado.infraestrutura.processo import sinalizar
from src.compartilhado.infraestrutura.unit_of_work import COLECAO_OUTBOX
from src.configuracao import ConfiguracaoDoRelay

if TYPE_CHECKING:
    import threading
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
        if canal.bloqueada:
            # Alarme do broker: nada de reivindicar; atender o broker traz o
            # Connection.Unblocked (ou a queda, quando o bloqueio vence).
            canal.aguardar(intervalo)
            continue
        try:
            lote_cheio = relay.entregar_pendentes(LOTE) == LOTE
        except PyMongoError:
            # Banco fora: reiniciar o pod nao o conserta; tenta de novo.
            _log.exception("relay_database_unavailable")
            lote_cheio = False
        if not lote_cheio:
            canal.aguardar(intervalo)


def main(parar: threading.Event | None = None) -> None:
    """Sobe o relay e roda ate o SIGTERM (``parar`` serve aos testes)."""
    configurar_logging()
    config = ConfiguracaoDoRelay.do_ambiente()
    with processo_da_mensageria(
        "relay", config, parar, exchanges=(EXCHANGE_EVENTOS,)
    ) as processo:
        coletor = ColetorDaOutbox(COLECAO_OUTBOX, processo.banco)
        REGISTRY.register(coletor)
        try:
            _log.info("relay_started")
            rodar(
                RelayDaOutbox(
                    processo.banco, processo.canal, usuario=config.rabbitmq_usuario
                ),
                processo.canal,
                parar=processo.parar,
                heartbeat=config.heartbeat,
            )
            _log.info("relay_stopped")
        finally:
            REGISTRY.unregister(coletor)


if __name__ == "__main__":  # pragma: no cover
    main()
