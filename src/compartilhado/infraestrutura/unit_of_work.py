"""Unidade de trabalho sobre ``ClientSession`` + transacao do MongoDB.

Requer replica set (transacoes multi-documento). A transacao usa read
concern ``snapshot`` e write concern ``majority``: duas transacoes que alteram
o mesmo documento geram ``WriteConflict`` (rotulo ``TransientTransactionError``)
na segunda, e o ``with_transaction`` do PyMongo reexecuta o trabalho do zero.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid7

from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern

from src.compartilhado.aplicacao.outbox import para_envelope
from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.mongo import aplicar_validador

if TYPE_CHECKING:
    from collections.abc import Callable

    from pymongo.client_session import ClientSession
    from pymongo.database import Database

    from src.compartilhado.dominio.aggregate_root import AggregateRoot
    from src.compartilhado.dominio.events import IntegrationEvent
    from src.compartilhado.infraestrutura.mongo import Documento

COLECAO_OUTBOX = "outbox"


class MongoUnitOfWork:
    def __init__(self, banco: Database[Documento]) -> None:
        self.banco = banco
        self._sessao: ClientSession | None = None
        self._agregados: list[AggregateRoot] = []
        self._eventos_avulsos: list[IntegrationEvent] = []

    @property
    def sessao(self) -> ClientSession | None:
        """Sessao da transacao corrente; ``None`` fora de ``executar``."""
        return self._sessao

    def executar[T](self, trabalho: Callable[[], T]) -> T:
        if self._sessao is not None:
            msg = "Transacao aninhada nao suportada"
            raise RuntimeError(msg)

        def tentativa(sessao: ClientSession) -> T:
            # Cada tentativa recomeca do zero: agregados de uma tentativa
            # abortada (e seus eventos) sao descartados com ela.
            self._sessao = sessao
            self._agregados.clear()
            self._eventos_avulsos.clear()
            resultado = trabalho()
            self._gravar_outbox(sessao)
            return resultado

        with self.banco.client.start_session() as sessao:
            try:
                resultado = sessao.with_transaction(
                    tentativa,
                    read_concern=ReadConcern("snapshot"),
                    write_concern=WriteConcern("majority"),
                    read_preference=ReadPreference.PRIMARY,
                )
            finally:
                self._sessao = None
        for agregado in self._agregados:
            agregado.limpar_eventos()
        self._agregados.clear()
        return resultado

    def registrar(self, agregado: AggregateRoot) -> None:
        """Chamado pelos repositorios ao salvar: os eventos vao para o outbox."""
        self._exigir_transacao()
        if agregado not in self._agregados:
            self._agregados.append(agregado)

    def registrar_evento(self, evento: IntegrationEvent) -> None:
        self._exigir_transacao()
        self._eventos_avulsos.append(evento)

    def _exigir_transacao(self) -> None:
        if self._sessao is None:
            msg = "Escrita fora de uma transacao: use UnitOfWork.executar"
            raise RuntimeError(msg)

    def _gravar_outbox(self, sessao: ClientSession) -> None:
        eventos = [
            *(ev for agregado in self._agregados for ev in agregado.coletar_eventos()),
            *self._eventos_avulsos,
        ]
        if not eventos:
            return
        criado_em = agora_utc()
        documentos = []
        for evento in eventos:
            # UUIDv7: ordenado pelo tempo e monotonico no mesmo milissegundo, o
            # relay publica em ordem de ``_id`` e eventos da mesma transacao
            # saem na ordem em que o dominio os registrou.
            mensagem_id = uuid7()
            documentos.append(
                {
                    "_id": mensagem_id,
                    "tipo": evento.tipo,
                    "status": "pendente",
                    "tentativas": 0,
                    "criado_em": criado_em,
                    "envelope": para_envelope(evento, mensagem_id=mensagem_id),
                }
            )
        self.banco[COLECAO_OUTBOX].insert_many(documentos, session=sessao)


ESQUEMA_OUTBOX: Documento = {
    "bsonType": "object",
    "required": ["tipo", "status", "tentativas", "criado_em", "envelope"],
    "properties": {
        "tipo": {"bsonType": "string"},
        "status": {"bsonType": "string"},
        "tentativas": {"bsonType": ["int", "long"], "minimum": 0},
        "criado_em": {"bsonType": "date"},
        "envelope": {"bsonType": "object"},
    },
}


def preparar_outbox(banco: Database[Documento]) -> None:
    aplicar_validador(banco, COLECAO_OUTBOX, ESQUEMA_OUTBOX)
    # Fila do relay: pendentes em ordem de ``_id`` (UUIDv7).
    banco[COLECAO_OUTBOX].create_index([("status", 1), ("_id", 1)])
