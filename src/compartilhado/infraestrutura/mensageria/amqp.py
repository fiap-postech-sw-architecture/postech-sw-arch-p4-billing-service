"""Conexao AMQP bloqueante (pika) do relay e do consumidor.

Um canal com publisher confirms: ``basic_publish`` com ``mandatory`` so volta
quando o broker confirma, e levanta erro na devolucao (sem rota) ou no nack.
A declaracao e sempre passiva: a topologia vem do ``definitions.json`` da
plataforma, e no RabbitMQ 4.3.6 a passiva tambem exige permissao no recurso,
entao cada processo confere so o que o usuario do servico alcanca (ADR-036).
"""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Final

import pika
from pika.exceptions import AMQPError, ChannelClosedByBroker, NackError, UnroutableError

from src.compartilhado.infraestrutura.processo import sinalizar

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Iterable
    from pathlib import Path

_log = logging.getLogger(__name__)

# Heartbeat curto acha conexao morta sem esperar o TCP; o bloqueio do broker
# (alarme de memoria ou disco) vira erro em vez de pendurar o processo.
_HEARTBEAT_SEGUNDOS: Final = 30
_BLOQUEIO_MAXIMO_SEGUNDOS: Final = 30
_TIMEOUT_SOCKET_SEGUNDOS: Final = 5
ESPERA_MAXIMA_SEGUNDOS: Final = 30.0


class MensagemRecusadaError(Exception):
    """O broker devolveu (sem rota), recusou (nack) ou fechou o canal por esta
    mensagem: e falha da mensagem, nao queda do broker."""


def parametros(url: str, *, nome: str) -> pika.URLParameters:
    """Parametros da conexao, com o nome que aparece no console do broker."""
    resultado = pika.URLParameters(url)
    resultado.heartbeat = _HEARTBEAT_SEGUNDOS
    resultado.blocked_connection_timeout = _BLOQUEIO_MAXIMO_SEGUNDOS
    resultado.socket_timeout = _TIMEOUT_SOCKET_SEGUNDOS
    resultado.connection_attempts = 1
    resultado.client_properties = {"connection_name": nome}
    return resultado


class CanalAmqp:
    """Conexao com um canal em modo confirm.

    ``abrir`` (re)conecta e confere por declaracao passiva as filas e os
    exchanges informados; erro de conexao (``AMQPConnectionError``) sobe para
    o laco do processo, que reconecta com backoff.
    """

    def __init__(
        self,
        parametros: pika.URLParameters,
        *,
        filas: Iterable[str] = (),
        exchanges: Iterable[str] = (),
    ) -> None:
        self._parametros = parametros
        self._filas = tuple(filas)
        self._exchanges = tuple(exchanges)
        self._conexao: Any = None
        self._canal: Any = None

    @property
    def canal(self) -> Any:  # noqa: ANN401 - BlockingChannel do pika, sem tipos
        """Canal aberto (consumo, ack e reject do consumidor)."""
        return self._canal

    def abrir(self) -> None:
        self.fechar()
        self._conexao = pika.BlockingConnection(self._parametros)
        self._abrir_canal()
        for fila in self._filas:
            self._canal.queue_declare(fila, passive=True)
        for exchange in self._exchanges:
            self._canal.exchange_declare(exchange, passive=True)

    def _abrir_canal(self) -> None:
        self._canal = self._conexao.channel()
        self._canal.confirm_delivery()

    def publicar(
        self,
        exchange: str,
        routing_key: str,
        corpo: bytes,
        propriedades: pika.BasicProperties,
    ) -> None:
        """Publica com ``mandatory`` e espera o confirm do broker.

        Raises:
            MensagemRecusadaError: devolvida sem rota, nack ou canal fechado
                pelo broker por causa dela (permissao, ``user_id``).
            AMQPError: conexao perdida (o chamador reconecta).
        """
        try:
            self._canal.basic_publish(
                exchange, routing_key, corpo, propriedades, mandatory=True
            )
        except (UnroutableError, NackError) as exc:
            raise MensagemRecusadaError(type(exc).__name__) from exc
        except ChannelClosedByBroker as exc:
            # O canal fica fechado: a proxima operacao falha e o laco do
            # processo reconecta (sem contar tentativa para outra mensagem).
            msg = f"canal fechado pelo broker ({exc.reply_code})"
            raise MensagemRecusadaError(msg) from exc

    def confirmar(self, entrega: int) -> None:
        self._canal.basic_ack(entrega)

    def rejeitar(self, entrega: int) -> None:
        """Sem requeue: o dead letter da fila leva a mensagem para a DLQ."""
        self._canal.basic_reject(entrega, requeue=False)

    def aguardar(self, segundos: float) -> None:
        """Atende o broker (heartbeat, fechamento) enquanto espera."""
        self._conexao.process_data_events(time_limit=segundos)

    def fechar(self) -> None:
        conexao, self._conexao, self._canal = self._conexao, None, None
        if conexao is not None and conexao.is_open:
            with suppress(AMQPError):
                conexao.close()


def manter_conectado(  # noqa: PLR0913 - laco dos dois processos, ajustavel nos testes
    canal: CanalAmqp,
    trabalho: Callable[[], None],
    *,
    parar: threading.Event,
    heartbeat: Path,
    processo: str,
    espera_maxima: float = ESPERA_MAXIMA_SEGUNDOS,
    cronometro: Callable[[], float] = time.monotonic,
) -> None:
    """Laco do relay e do consumidor: conecta, roda ``trabalho`` enquanto a
    conexao durar e reconecta com backoff dobrado ate ``espera_maxima``.

    Sem conexao o ``trabalho`` nao roda (o relay nao reivindica linhas). A
    conexao que cai logo depois de aberta (o broker fechando o canal a cada
    mensagem, por permissao, ou cancelando o consumidor) tambem espera antes de
    reconectar; so a que durou ``espera_maxima`` volta a reconectar na hora. O
    arquivo de vida fica ``conectando`` fora do ``trabalho``, que o marca
    ``pronto`` a cada volta.
    """
    espera = min(1.0, espera_maxima)
    while not parar.is_set():
        sinalizar(heartbeat, pronto=False)
        try:
            canal.abrir()
        except AMQPError:
            _log.warning(
                "broker_unavailable",
                extra={"processo": processo, "espera_segundos": espera},
            )
        else:
            _log.info("broker_connected", extra={"processo": processo})
            inicio = cronometro()
            try:
                trabalho()
                if not parar.is_set():
                    # O consume() do pika encerra o gerador, sem excecao, quando
                    # o broker cancela o consumidor (fila apagada, failover).
                    _log.warning(
                        "broker_cancelled_consumer", extra={"processo": processo}
                    )
            except AMQPError:
                _log.warning("broker_connection_lost", extra={"processo": processo})
            finally:
                canal.fechar()
            if cronometro() - inicio >= espera_maxima:
                espera = min(1.0, espera_maxima)
                continue
        parar.wait(espera)
        espera = min(espera * 2, espera_maxima)
    sinalizar(heartbeat, pronto=False)
