"""Relay da outbox para o RabbitMQ (ADR-036; RFC-004, secao 5.4).

O MongoDB nao tem ``LISTEN/NOTIFY`` nem ``SKIP LOCKED``: o relay faz polling e
reivindica cada linha com claim atomico (``find_one_and_update`` de pendente,
ou de em entrega com lease vencido, para em entrega), com lease de 30 s e um
token de reivindicacao. Toda marcacao posterior exige o token (fencing): um
relay atrasado, cujo lease venceu e outro relay retomou, nao marca entregue o
que nao e mais dele.

A linha so vira entregue depois do confirm do broker. Devolucao sem rota
(``mandatory``), nack ou canal fechado pela mensagem contam tentativa, com os
atrasos do relay do p3 ate ``dead``; queda do broker nao conta: a linha volta
a pendente na hora, e o laco do processo reconecta antes de reivindicar outra.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID, uuid4

import pika
from opentelemetry.trace import SpanKind, Status, StatusCode
from pika.exceptions import AMQPError
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.mensageria.amqp import MensagemRecusadaError
from src.compartilhado.infraestrutura.mensageria.metricas import MENSAGENS_PUBLICADAS
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_atual,
    contexto_de,
    links_de,
    tracer,
)
from src.compartilhado.infraestrutura.unit_of_work import COLECAO_OUTBOX

if TYPE_CHECKING:
    from pymongo.database import Database

    from src.compartilhado.dominio.relogio import Relogio
    from src.compartilhado.infraestrutura.mensageria.amqp import Publicador
    from src.compartilhado.infraestrutura.mongo import Documento

_log = logging.getLogger(__name__)

LEASE: Final = timedelta(seconds=30)
# Politica do relay do p3 (relay/backoff.py): a N-esima falha espera
# ATRASOS[N-1]; a quinta vira dead.
ATRASOS_SEGUNDOS: Final = (1, 4, 16, 64)
MAX_TENTATIVAS: Final = len(ATRASOS_SEGUNDOS) + 1
_EM_ENTREGA: Final = "em_entrega"


@dataclass(frozen=True, slots=True)
class _Linha:
    id: UUID
    tipo: str
    tentativas: int
    exchange: str
    routing_key: str
    envelope: dict[str, Any]
    contexto: dict[str, str]
    retomado_por: dict[str, str]
    reivindicacao: UUID

    @classmethod
    def de(cls, doc: Documento) -> _Linha:
        return cls(
            id=doc["_id"],
            tipo=doc["tipo"],
            tentativas=doc["tentativas"],
            exchange=doc["exchange"],
            routing_key=doc["routing_key"],
            envelope=doc["envelope"],
            contexto={k: doc[k] for k in ("traceparent", "tracestate") if k in doc},
            retomado_por=doc.get("retomado_por", {}),
            reivindicacao=doc["reivindicacao"],
        )

    @property
    def rotulos(self) -> dict[str, str]:
        return {
            "tipo": self.tipo,
            "mensagem_id": str(self.id),
            "correlation_id": str(self.envelope["correlation_id"]),
        }


class RelayDaOutbox:
    """Entrega as linhas pendentes da outbox, uma de cada vez, em ordem."""

    def __init__(
        self,
        banco: Database[Documento],
        publicador: Publicador,
        *,
        usuario: str,
        relogio: Relogio = agora_utc,
        lease: timedelta = LEASE,
    ) -> None:
        self._outbox = banco[COLECAO_OUTBOX]
        self._publicador = publicador
        self._usuario = usuario
        self._relogio = relogio
        self._lease = lease

    def entregar_pendentes(self, limite: int) -> int:
        """Reivindica e entrega ate ``limite`` linhas; devolve quantas pegou.

        Sem linha elegivel nao abre span (laco ocioso sem ruido no Jaeger).
        """
        reivindicadas = 0
        while reivindicadas < limite:
            linha = self._reivindicar()
            if linha is None:
                break
            reivindicadas += 1
            self._entregar(linha)
        return reivindicadas

    def _reivindicar(self) -> _Linha | None:
        agora = self._relogio()
        doc = self._outbox.find_one_and_update(
            {
                "status": {"$in": ["pendente", _EM_ENTREGA]},
                "proxima_tentativa_em": {"$lte": agora},
            },
            {
                "$set": {
                    "status": _EM_ENTREGA,
                    "proxima_tentativa_em": agora + self._lease,
                    "reivindicacao": uuid4(),
                }
            },
            sort=[("proxima_tentativa_em", 1), ("_id", 1)],
            return_document=ReturnDocument.AFTER,
        )
        return None if doc is None else _Linha.de(doc)

    def _entregar(self, linha: _Linha) -> None:
        # Publica no contexto gravado com a linha: o span PRODUCER e filho de
        # quem gravou (consumidor do comando ou registro que esperava), com span
        # link para quem retomou o passo que esperava (ADR-043).
        with tracer.start_as_current_span(
            f"publish {linha.tipo}",
            context=contexto_de(linha.contexto),
            kind=SpanKind.PRODUCER,
            links=links_de(linha.retomado_por),
            attributes={
                "messaging.system": "rabbitmq",
                "messaging.operation.type": "publish",
                "messaging.destination.name": linha.exchange,
                "messaging.rabbitmq.destination.routing_key": linha.routing_key,
                "messaging.message.id": str(linha.id),
                "messaging.message.conversation_id": linha.rotulos["correlation_id"],
            },
        ) as span:
            try:
                self._publicador.publicar(
                    linha.exchange,
                    linha.routing_key,
                    json.dumps(linha.envelope, separators=(",", ":")).encode(),
                    self._propriedades(linha),
                )
            except MensagemRecusadaError as exc:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                self._contar_falha(linha, str(exc))
                return
            except AMQPError:
                # Queda do broker: devolve sem gastar tentativa e deixa o laco
                # do processo reconectar.
                self._devolver(linha)
                raise
            except Exception as exc:  # noqa: BLE001 - defeito: conta ate dead (alerta)
                # Sem contar, a linha voltaria a cada lease e o processo cairia
                # nela para sempre; contando, vira dead e aparece em outbox_dead.
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                _log.exception("outbox_publish_failed", extra=linha.rotulos)
                self._contar_falha(linha, type(exc).__name__)
                return
        MENSAGENS_PUBLICADAS.labels(tipo=linha.tipo).inc()
        if self._marcar_entregue(linha):
            _log.info("outbox_message_published", extra=linha.rotulos)
        else:
            # Lease vencido e retomado por outro relay: a linha e dele agora (a
            # mensagem pode sair duas vezes; o consumidor deduplica pelo id).
            _log.warning("outbox_claim_lost", extra=linha.rotulos)

    def _propriedades(self, linha: _Linha) -> pika.BasicProperties:
        # Sem x-tentativa na primeira entrega (ele so existe na copia de retry).
        return pika.BasicProperties(
            message_id=str(linha.id),
            correlation_id=linha.rotulos["correlation_id"],
            type=linha.tipo,
            user_id=self._usuario,
            content_type="application/json",
            delivery_mode=pika.DeliveryMode.Persistent,
            headers=contexto_atual(),
        )

    def _da_reivindicacao(self, linha: _Linha) -> Documento:
        # Fencing: so quem ainda detem a reivindicacao muda a linha.
        return {
            "_id": linha.id,
            "status": _EM_ENTREGA,
            "reivindicacao": linha.reivindicacao,
        }

    def _marcar_entregue(self, linha: _Linha) -> bool:
        resultado = self._outbox.update_one(
            self._da_reivindicacao(linha),
            {
                "$set": {"status": "entregue", "entregue_em": self._relogio()},
                "$unset": {"reivindicacao": ""},
            },
        )
        return resultado.modified_count == 1

    def _contar_falha(self, linha: _Linha, erro: str) -> None:
        tentativas = linha.tentativas + 1
        mudanca: Documento = {"tentativas": tentativas, "ultimo_erro": erro}
        if tentativas >= MAX_TENTATIVAS:
            mudanca["status"] = "dead"
        else:
            atraso = timedelta(seconds=ATRASOS_SEGUNDOS[tentativas - 1])
            mudanca |= {
                "status": "pendente",
                "proxima_tentativa_em": self._relogio() + atraso,
            }
        resultado = self._outbox.update_one(
            self._da_reivindicacao(linha),
            {"$set": mudanca, "$unset": {"reivindicacao": ""}},
        )
        contexto = {**linha.rotulos, "tentativas": tentativas, "erro": erro}
        if resultado.modified_count == 0:
            # Lease retomado por outro relay: a falha nao e mais desta linha.
            _log.warning("outbox_claim_lost", extra=linha.rotulos)
        elif mudanca["status"] == "dead":
            _log.error("outbox_message_dead", extra=contexto)
        else:
            _log.warning("outbox_message_refused", extra=contexto)

    def _devolver(self, linha: _Linha) -> None:
        try:
            self._outbox.update_one(
                self._da_reivindicacao(linha),
                {
                    "$set": {
                        "status": "pendente",
                        "proxima_tentativa_em": self._relogio(),
                    },
                    "$unset": {"reivindicacao": ""},
                },
            )
        except PyMongoError:
            # Sem o banco, o lease devolve a linha sozinho quando vencer.
            _log.warning("outbox_claim_release_failed", extra=linha.rotulos)
