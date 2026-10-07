"""Conexao AMQP bloqueante (pika) do relay e do consumidor.

Publicacao num canal proprio com publisher confirms: ``basic_publish`` com
``mandatory`` so volta quando o broker confirma, e levanta erro na devolucao
(sem rota) ou no nack. Separado do canal de consumo, o canal que o broker fecha
por causa de uma publicacao (permissao, ``user_id``) nao leva junto o ack e o
reject das mensagens consumidas; ele e reaberto na publicacao seguinte. A
declaracao e sempre passiva: a topologia vem do ``definitions.json`` da
plataforma, e no RabbitMQ 4.3.6 a passiva tambem exige permissao no recurso,
entao cada processo confere so o que o usuario do servico alcanca (ADR-036).
"""

from __future__ import annotations

import logging
import secrets
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Final, Protocol

import pika
from pika.exceptions import AMQPError, ChannelClosedByBroker, NackError, UnroutableError

from src.compartilhado.infraestrutura.processo import sinalizar

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Iterable, Iterator
    from pathlib import Path

_log = logging.getLogger(__name__)

# Heartbeat curto acha conexao morta sem esperar o TCP; o bloqueio do broker
# (alarme de memoria ou disco) vira erro em vez de pendurar o processo.
_HEARTBEAT_SEGUNDOS: Final = 30
_BLOQUEIO_MAXIMO_SEGUNDOS: Final = 30
_TIMEOUT_SOCKET_SEGUNDOS: Final = 5
ESPERA_MAXIMA_SEGUNDOS: Final = 30.0
# Jitter da reconexao: replicas que perderam o broker juntas nao voltam juntas.
# A espera fica entre a metade e o total do atraso (nunca reconexao imediata).
_aleatorio = secrets.SystemRandom()


class MensagemRecusadaError(Exception):
    """O broker devolveu (sem rota), recusou (nack) ou fechou o canal por esta
    mensagem: e falha da mensagem, nao queda do broker."""

    @property
    def detalhe_de_log(self) -> dict[str, str]:
        """Texto fixo (nome do erro ou codigo do broker), nunca a mensagem."""
        return {"detalhe": str(self)}


class Publicador(Protocol):
    """Quem publica com confirm do broker (o relay so precisa disto)."""

    def publicar(
        self,
        exchange: str,
        routing_key: str,
        corpo: bytes,
        propriedades: pika.BasicProperties,
    ) -> None:
        """Volta so com o confirm; ``MensagemRecusadaError`` se o broker recusar."""
        ...


class Canal(Publicador, Protocol):
    """O canal do consumidor: publica a copia de retry, confirma ou rejeita a
    entrega e atende o broker enquanto o handler roda."""

    def confirmar(self, entrega: int) -> None:
        """Ack da entrega."""
        ...

    def rejeitar(self, entrega: int) -> None:
        """Reject sem requeue: a fila manda a mensagem para a DLQ."""
        ...

    def aguardar(self, segundos: float) -> None:
        """Atende o broker (heartbeat, fechamento) por ate ``segundos``."""
        ...


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
    """Conexao com o canal de consumo e o de publicacao (modo confirm).

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
        # Objetos do pika, que nao publica tipos: conexao, canal de consumo e
        # canal de publicacao.
        self._conexao: Any = None
        self._canal: Any = None
        self._publicacao: Any = None

    def abrir(self) -> None:
        """Conecta (fechando a conexao anterior) e confere, por declaracao
        passiva, as filas e os exchanges que o processo usa."""
        self.fechar()
        self._conexao = pika.BlockingConnection(self._parametros)
        self._canal = self._conexao.channel()
        for fila in self._filas:
            self._canal.queue_declare(fila, passive=True)
        for exchange in self._exchanges:
            self._canal.exchange_declare(exchange, passive=True)

    def _canal_de_publicacao(self) -> Any:  # noqa: ANN401 - BlockingChannel do pika
        if self._publicacao is None or not self._publicacao.is_open:
            self._publicacao = self._conexao.channel()
            self._publicacao.confirm_delivery()
        return self._publicacao

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
        canal = self._canal_de_publicacao()
        try:
            canal.basic_publish(
                exchange, routing_key, corpo, propriedades, mandatory=True
            )
        except (UnroutableError, NackError) as exc:
            # Texto fixo: a excecao do pika carrega a mensagem devolvida.
            raise MensagemRecusadaError(type(exc).__name__) from exc
        except ChannelClosedByBroker as exc:
            # So o canal de publicacao fecha; a proxima publicacao o reabre.
            msg = f"canal fechado pelo broker ({exc.reply_code})"
            raise MensagemRecusadaError(msg) from exc

    def consumir(
        self, fila: str, *, prefetch: int, inatividade: float
    ) -> Iterator[tuple[int, pika.BasicProperties, bytes] | None]:
        """Entregas da fila (tag, propriedades e corpo), com ``None`` a cada
        ``inatividade`` segundos sem mensagem. O gerador acaba, sem excecao,
        quando o broker cancela o consumo (fila apagada, failover)."""
        self._canal.basic_qos(prefetch_count=prefetch)
        for metodo, propriedades, corpo in self._canal.consume(
            fila, inactivity_timeout=inatividade
        ):
            yield None if metodo is None else (metodo.delivery_tag, propriedades, corpo)

    def cancelar_consumo(self) -> None:
        """Para de consumir: as entregas pre-buscadas sem ack voltam a fila."""
        self._canal.cancel()

    def confirmar(self, entrega: int) -> None:
        """Ack da entrega no canal de consumo."""
        self._canal.basic_ack(entrega)

    def rejeitar(self, entrega: int) -> None:
        """Sem requeue: o dead letter da fila leva a mensagem para a DLQ."""
        self._canal.basic_reject(entrega, requeue=False)

    def aguardar(self, segundos: float) -> None:
        """Atende o broker (heartbeat, fechamento) enquanto espera."""
        self._conexao.process_data_events(time_limit=segundos)

    def fechar(self) -> None:
        """Fecha a conexao, se aberta (sem erro se ela ja caiu)."""
        conexao, self._conexao = self._conexao, None
        self._canal = self._publicacao = None
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
    conexao durar e reconecta com backoff dobrado ate ``espera_maxima``, com
    jitter.

    Sem conexao o ``trabalho`` nao roda (o relay nao reivindica linhas). A
    conexao que cai logo depois de aberta (o broker fechando o canal a cada
    mensagem, por permissao, ou cancelando o consumidor) tambem espera antes de
    reconectar; so a que durou ``espera_maxima`` volta a reconectar na hora.
    """
    espera = min(1.0, espera_maxima)
    while not parar.is_set():
        duracao = _conectar_e_trabalhar(
            canal,
            trabalho,
            parar=parar,
            heartbeat=heartbeat,
            processo=processo,
            cronometro=cronometro,
        )
        if duracao is not None and duracao >= espera_maxima:
            espera = min(1.0, espera_maxima)
            continue
        parar.wait(espera * _aleatorio.uniform(0.5, 1.0))
        espera = min(espera * 2, espera_maxima)
    sinalizar(heartbeat, pronto=False)


def _conectar_e_trabalhar(
    canal: CanalAmqp,
    trabalho: Callable[[], None],
    *,
    parar: threading.Event,
    heartbeat: Path,
    processo: str,
    cronometro: Callable[[], float],
) -> float | None:
    """Uma conexao: abre, roda o ``trabalho`` enquanto ela durar e fecha.

    Devolve quanto ela durou (``None`` quando nem abriu). O arquivo de vida so
    diz ``pronto`` dentro do ``trabalho``: fora dele, inclusive na espera antes
    da proxima tentativa, diz ``conectando`` (readiness falsa, liveness pela
    idade do arquivo).
    """
    sinalizar(heartbeat, pronto=False)
    try:
        canal.abrir()
    except AMQPError as exc:
        # So o tipo: autenticacao recusada ou 403/404 na declaracao passiva nao
        # se confundem com o broker fora do ar.
        _log.warning(
            "broker_unavailable",
            extra={"processo": processo, "erro": type(exc).__name__},
        )
        return None
    _log.info("broker_connected", extra={"processo": processo})
    inicio = cronometro()
    try:
        trabalho()
        if not parar.is_set():
            # O consume() do pika encerra o gerador, sem excecao, quando o
            # broker cancela o consumidor (fila apagada, failover).
            _log.warning("broker_cancelled_consumer", extra={"processo": processo})
    except AMQPError as exc:
        _log.warning(
            "broker_connection_lost",
            extra={"processo": processo, "erro": type(exc).__name__},
        )
    finally:
        canal.fechar()
        sinalizar(heartbeat, pronto=False)
    return cronometro() - inicio
