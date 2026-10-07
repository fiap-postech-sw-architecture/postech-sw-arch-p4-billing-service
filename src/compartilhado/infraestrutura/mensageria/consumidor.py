"""Consumidor dos comandos da saga (ADR-036; RFC-004, secoes 5.1 e 5.4).

Para cada mensagem, antes de qualquer campo dela ir para o log ou para o span:
``x-tentativa``, corpo de ate 64 KiB, JSON, tipo com handler, ``user_id`` do
produtor do tipo (comandos sao do ``os``; a copia de retry chega com o proprio
usuario e ``x-tentativa`` maior que zero) e envelope no contrato. Qualquer
falha nessa leitura, por qualquer motivo, vai para a DLQ: nenhuma excecao
causada pela mensagem sai de ``tratar``. Depois, o span CONSUMER filho da
publicacao e o handler do tipo na transacao da mensagem: o handler grava o
efeito sem comitar, e o consumidor comita junto a outbox e
``mensagens_processadas`` (ou nada, se algo falhar). O handler roda mesmo para
o ``id`` ja visto: os casos de uso sao idempotentes pela chave de negocio e
republicam o desfecho registrado, sem repetir o efeito.

Erro transitorio (banco, provedor ou rede fora; erro que o MongoDB marca como
repetivel): copia publicada no ``pytstop.retry`` com ``x-tentativa``
incrementado, na fila de retry do nivel da nova tentativa (``<fila>.retry.1s``
a ``.300s``, cada uma com o proprio TTL), com confirm, e so entao ack na
original. A sexta falha, a copia recusada e qualquer outro erro (regra de
negocio sem evento de falha no contrato ou defeito) vao para a DLQ.
"""

from __future__ import annotations

import contextvars
import json
import logging
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol
from uuid import UUID

import pika
import structlog
from opentelemetry.trace import SpanKind, Status, StatusCode
from pika.exceptions import AMQPError
from pymongo.errors import ConnectionFailure, PyMongoError

from src.compartilhado.dominio.exceptions import (
    DependenciaIndisponivelError,
    DomainException,
)
from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.mensageria import contratos
from src.compartilhado.infraestrutura.mensageria.amqp import MensagemRecusadaError
from src.compartilhado.infraestrutura.mensageria.metricas import (
    MENSAGENS_CONSUMIDAS,
    TIPO_DESCONHECIDO,
)
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_atual,
    contexto_de,
    tracer,
)
from src.compartilhado.infraestrutura.unit_of_work import (
    COLECAO_PROCESSADAS,
    MensagemRecebida,
    processar_mensagem,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pymongo.database import Database

    from src.compartilhado.dominio.relogio import Relogio
    from src.compartilhado.infraestrutura.mongo import Documento
    from src.compartilhado.infraestrutura.unit_of_work import UnidadeDaMensagem

_log = logging.getLogger(__name__)

EXCHANGE_RETRY: Final = "pytstop.retry"
# O envelope real tem poucos KB; acima disso o corpo nem chega ao parser.
TAMANHO_MAXIMO_DO_CORPO: Final = 64 * 1024
# Campo ainda nao validado vai para o log cortado.
_LIMITE_NO_LOG: Final = 64
# Enquanto o handler roda na thread de trabalho, a da conexao atende o broker
# (heartbeat, bloqueio, fechamento) a cada meio segundo.
_ATENDER_O_BROKER_A_CADA_SEGUNDOS: Final = 0.5
# Fila de retry por tentativa (1 a 5); a sexta falha vai para a DLQ.
NIVEIS_DE_RETRY: Final = ("1s", "5s", "15s", "60s", "300s")
PRODUTOR_DOS_COMANDOS: Final = "os"
# Rotulos que o proprio MongoDB poe no erro que vale repetir.
_ROTULOS_TRANSITORIOS: Final = (
    "TransientTransactionError",
    "RetryableWriteError",
    "UnknownTransactionCommitResult",
)


class Desfecho(StrEnum):
    """O que o handler fez com o comando (rotulo ``resultado`` da metrica)."""

    PROCESSADA = "processada"
    # Original atrasado depois da lapide: descartado sem efeito e sem resposta.
    IGNORADA = "ignorada"


type Handler = Callable[[Mapping[str, Any], UnidadeDaMensagem], Desfecho]


class Canal(Protocol):
    def publicar(
        self,
        exchange: str,
        routing_key: str,
        corpo: bytes,
        propriedades: pika.BasicProperties,
    ) -> None: ...

    def confirmar(self, entrega: int) -> None: ...

    def rejeitar(self, entrega: int) -> None: ...

    def aguardar(self, segundos: float) -> None: ...


class _PermanenteError(Exception):
    """Mensagem que nenhuma repeticao conserta: vai direto para a DLQ."""


@dataclass(frozen=True, slots=True)
class _Entrega:
    tag: int
    propriedades: pika.BasicProperties
    cabecalhos: dict[str, Any]
    corpo: bytes
    tentativa: int
    tipo: str


class ConsumidorDeComandos:
    """Trata cada entrega da fila: ack, copia de retry ou DLQ."""

    def __init__(
        self,
        banco: Database[Documento],
        handlers: Mapping[str, Handler],
        *,
        fila: str,
        usuario: str,
        relogio: Relogio = agora_utc,
    ) -> None:
        self._banco = banco
        self._handlers = handlers
        self._fila = fila
        self._usuario = usuario
        self._relogio = relogio
        # O handler roda fora da thread da conexao: a do pika so atende o
        # broker quando o codigo dela volta ao pika, e um handler mais lento que
        # o heartbeat derrubaria a conexao no meio da mensagem.
        self._trabalho = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="consumidor-handler"
        )

    def fechar(self) -> None:
        """Espera o handler em curso e encerra a thread de trabalho."""
        self._trabalho.shutdown()

    def tratar(
        self,
        canal: Canal,
        tag: int,
        propriedades: pika.BasicProperties,
        corpo: bytes,
    ) -> str:
        """Processa uma entrega; devolve o ``resultado`` contado na metrica.

        So a queda do broker (erro do canal ao confirmar, rejeitar ou publicar)
        sobe para o laco do processo, que reconecta.
        """
        cabecalhos: dict[str, Any] = dict(propriedades.headers or {})
        tipo = TIPO_DESCONHECIDO
        try:
            tentativa = _tentativa(cabecalhos)
            envelope = _envelope(corpo)
            tipo = self._tipo_conhecido(envelope)
            self._conferir_origem(propriedades.user_id, tentativa)
            contratos.validar(envelope)
        except Exception as exc:  # noqa: BLE001 - entrada fora da regra, por qualquer motivo, vai para a DLQ
            resultado = self._descartar(canal, tag, exc, **_identificacao(propriedades))
        else:
            entrega = _Entrega(tag, propriedades, cabecalhos, corpo, tentativa, tipo)
            with structlog.contextvars.bound_contextvars(
                correlation_id=envelope["correlation_id"], mensagem_id=envelope["id"]
            ):
                resultado = self._tratar_no_span(canal, entrega, envelope)
        MENSAGENS_CONSUMIDAS.labels(tipo=tipo, resultado=resultado).inc()
        return resultado

    def _tipo_conhecido(self, envelope: Mapping[str, Any]) -> str:
        tipo = envelope.get("tipo")
        if not isinstance(tipo, str) or tipo not in self._handlers:
            msg = f"tipo {_curto(repr(tipo))} sem handler neste consumidor"
            raise _PermanenteError(msg)
        return tipo

    def _conferir_origem(self, usuario: str | None, tentativa: int) -> None:
        # Defesa em profundidade: o broker ja confere user_id x conexao, e as
        # permissoes de topico limitam quem publica cada routing key.
        if usuario == PRODUTOR_DOS_COMANDOS or (
            tentativa > 0 and usuario == self._usuario
        ):
            return
        msg = "user_id diferente do produtor do tipo"
        raise _PermanenteError(msg)

    def _tratar_no_span(
        self, canal: Canal, entrega: _Entrega, envelope: Mapping[str, Any]
    ) -> str:
        with tracer.start_as_current_span(
            f"process {entrega.tipo}",
            context=contexto_de(entrega.cabecalhos),
            kind=SpanKind.CONSUMER,
            attributes={
                "messaging.system": "rabbitmq",
                "messaging.operation.type": "process",
                "messaging.destination.name": self._fila,
                "messaging.message.id": envelope["id"],
                "messaging.message.conversation_id": envelope["correlation_id"],
                "pytstop.tentativa": entrega.tentativa,
            },
        ) as span:
            try:
                resultado = self._processar_fora_da_conexao(canal, envelope)
            except AMQPError:
                # A conexao caiu com o handler rodando: o laco reconecta e o
                # broker devolve a mensagem (o handler e idempotente).
                raise
            except Exception as exc:  # noqa: BLE001 - classificado abaixo; nunca ack mudo
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                if _transitorio(exc):
                    return self._repetir(canal, entrega, exc)
                return self._descartar(canal, entrega.tag, exc, tipo=entrega.tipo)
            canal.confirmar(entrega.tag)
            _log.info(
                "command_consumed",
                extra={
                    "tipo": entrega.tipo,
                    "resultado": resultado,
                    "tentativa": entrega.tentativa,
                },
            )
            return resultado

    def _processar_fora_da_conexao(
        self, canal: Canal, envelope: Mapping[str, Any]
    ) -> str:
        # No contexto deste span (logs e outbox seguem o trace da mensagem).
        futuro = self._trabalho.submit(
            contextvars.copy_context().run, self._processar, envelope
        )
        while not wait([futuro], timeout=_ATENDER_O_BROKER_A_CADA_SEGUNDOS).done:
            canal.aguardar(0)
        return futuro.result()

    def _processar(self, envelope: Mapping[str, Any]) -> str:
        mensagem = MensagemRecebida(
            id=UUID(envelope["id"]),
            tipo=envelope["tipo"],
            correlation_id=UUID(envelope["correlation_id"]),
        )
        duplicada = (
            self._banco[COLECAO_PROCESSADAS].find_one({"_id": mensagem.id}, {"_id": 1})
            is not None
        )
        handler, dados = self._handlers[mensagem.tipo], envelope["dados"]
        # O consumidor comita: efeito, outbox e mensagens_processadas juntos.
        desfecho = processar_mensagem(
            self._banco,
            mensagem,
            lambda uow: handler(dados, uow),
            relogio=self._relogio,
        )
        return "duplicada" if duplicada else desfecho.value

    def _repetir(self, canal: Canal, entrega: _Entrega, erro: Exception) -> str:
        proxima = entrega.tentativa + 1
        if proxima > len(NIVEIS_DE_RETRY):
            return self._descartar(canal, entrega.tag, erro, tipo=entrega.tipo)
        original = entrega.propriedades
        copia = pika.BasicProperties(
            message_id=original.message_id,
            correlation_id=original.correlation_id,
            type=original.type,
            # O broker exige o usuario da conexao; o consumidor aceita o proprio
            # usuario de volta porque x-tentativa > 0.
            user_id=self._usuario,
            content_type="application/json",
            delivery_mode=pika.DeliveryMode.Persistent,
            headers={**entrega.cabecalhos, **contexto_atual(), "x-tentativa": proxima},
        )
        fila_de_retry = f"{self._fila}.retry.{NIVEIS_DE_RETRY[proxima - 1]}"
        try:
            canal.publicar(EXCHANGE_RETRY, fila_de_retry, entrega.corpo, copia)
        except MensagemRecusadaError as exc:
            # Sem a copia confirmada, a original nao pode sumir: vai para a DLQ.
            return self._descartar(canal, entrega.tag, exc, tipo=entrega.tipo)
        canal.confirmar(entrega.tag)
        _log.warning(
            "command_retry_scheduled",
            extra={
                "tipo": entrega.tipo,
                "tentativa": proxima,
                "erro": type(erro).__name__,
            },
        )
        return "retry"

    def _descartar(
        self, canal: Canal, tag: int, erro: Exception, **identificacao: str | None
    ) -> str:
        canal.rejeitar(tag)
        contexto: dict[str, Any] = {**identificacao, "erro": type(erro).__name__}
        if isinstance(erro, DomainException):
            # So o codigo: a mensagem pode trazer texto do provedor de pagamento.
            _log.error(
                "command_dead_lettered", extra={**contexto, "codigo": erro.codigo}
            )
        elif isinstance(
            erro,
            (
                _PermanenteError,
                MensagemRecusadaError,
                contratos.MensagemForaDoContratoError,
            ),
        ):
            # Textos fixos ou o ponto do contrato que falhou, nunca o dado.
            _log.error(
                "command_dead_lettered", extra={**contexto, "detalhe": str(erro)}
            )
        else:
            _log.error("command_dead_lettered", extra=contexto, exc_info=erro)
        return "dlq"


def _transitorio(erro: Exception) -> bool:
    """Falha que passa sozinha: provedor ou rede fora, banco inacessivel ou
    erro que o MongoDB marca como repetivel. O resto (contrato, regra, defeito,
    documento recusado pelo validador do banco) nao melhora repetindo."""
    if isinstance(erro, (DependenciaIndisponivelError, ConnectionError, TimeoutError)):
        return True
    if not isinstance(erro, PyMongoError):
        return False
    return (
        isinstance(erro, ConnectionFailure)
        or erro.timeout
        or any(erro.has_error_label(rotulo) for rotulo in _ROTULOS_TRANSITORIOS)
    )


def _tentativa(cabecalhos: Mapping[str, Any]) -> int:
    valor = cabecalhos.get("x-tentativa", 0)
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
        msg = f"x-tentativa invalido: {_curto(repr(valor))}"
        raise _PermanenteError(msg)
    return valor


def _envelope(corpo: bytes) -> dict[str, Any]:
    if len(corpo) > TAMANHO_MAXIMO_DO_CORPO:
        msg = f"corpo de {len(corpo)} bytes, acima de {TAMANHO_MAXIMO_DO_CORPO}"
        raise _PermanenteError(msg)
    try:
        envelope = json.loads(corpo)
    except (ValueError, RecursionError):
        # RecursionError: JSON aninhado alem do limite do parser.
        msg = "corpo nao e JSON"
        raise _PermanenteError(msg) from None
    if not isinstance(envelope, dict):
        msg = "corpo nao e um envelope"
        raise _PermanenteError(msg)
    return envelope


def _identificacao(propriedades: pika.BasicProperties) -> dict[str, str | None]:
    """O que acha na DLQ a mensagem descartada antes da validacao, cortado (as
    propriedades AMQP repetem o envelope, mas ainda ninguem as conferiu)."""
    return {
        "tipo": _curto(propriedades.type),
        "mensagem_id": _curto(propriedades.message_id),
        "correlation_id": _curto(propriedades.correlation_id),
        "user_id": _curto(propriedades.user_id),
    }


def _curto(valor: object) -> str | None:
    return None if valor is None else str(valor)[:_LIMITE_NO_LOG]
