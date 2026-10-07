"""Unidade de trabalho sobre ``ClientSession`` + transacao do MongoDB.

Requer replica set (transacoes multi-documento). A transacao usa read
concern ``snapshot`` e write concern ``majority``: duas transacoes que alteram
o mesmo documento geram ``WriteConflict`` (rotulo ``TransientTransactionError``)
na segunda, e o ``with_transaction`` do PyMongo reexecuta o trabalho do zero.

Mensageria (ADR-036; RFC-004, secao 5.4): cada evento vira um documento da
outbox com o envelope validado no contrato, o exchange, a routing key e o
contexto W3C (``traceparent``) de quem gravou, na mesma transacao do efeito.
Durante um comando da saga (``mensagem``), o id dele e o ``causation_id`` das
respostas e entra em ``mensagens_processadas`` na mesma transacao; o agregado
criado guarda em ``aberto_por`` o comando e o contexto de trace que o abriram,
de onde saem a causa e o trace dos eventos que ele emite depois, sem comando
(decisao do cliente, webhook, prazo; ADR-043).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Final
from uuid import uuid7

from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern

from src.compartilhado.aplicacao.outbox import para_envelope
from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.mensageria import contratos
from src.compartilhado.infraestrutura.mensageria.telemetria import contexto_atual
from src.compartilhado.infraestrutura.mongo import aplicar_validador

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from uuid import UUID

    from pymongo.client_session import ClientSession
    from pymongo.database import Database

    from src.compartilhado.dominio.aggregate_root import AggregateRoot
    from src.compartilhado.dominio.events import IntegrationEvent
    from src.compartilhado.dominio.relogio import Relogio
    from src.compartilhado.infraestrutura.mongo import Documento

COLECAO_OUTBOX: Final = "outbox"
COLECAO_PROCESSADAS: Final = "mensagens_processadas"
# Retencao (RFC-004, secao 10.3): entregue some em 7 dias; a janela de
# idempotencia (30 dias) passa da DLQ (7 dias) e da janela de reenvio.
RETENCAO_OUTBOX: Final = timedelta(days=7)
RETENCAO_PROCESSADAS: Final = timedelta(days=30)
_CONTEXTO_W3C: Final = ("traceparent", "tracestate")


@dataclass(frozen=True, slots=True)
class MensagemRecebida:
    """Comando da saga em processamento: causa das respostas e chave da
    idempotencia do consumidor."""

    id: UUID
    tipo: str
    correlation_id: UUID


@dataclass(frozen=True, slots=True)
class _Causa:
    mensagem_id: UUID | None
    contexto: dict[str, str] = field(default_factory=dict)


class MongoUnitOfWork:
    """Escritas de um caso de uso e os eventos delas na mesma transacao.

    Os repositorios gravam com ``gravar`` (ou ``sessao`` e ``registrar``); ao
    fim do trabalho, os eventos dos agregados registrados (e os avulsos, como a
    resposta republicada na repeticao) viram documentos da outbox na mesma
    transacao (RFC-004, secao 5.2). Nenhum evento sai sem a escrita, nem o
    contrario.
    """

    def __init__(
        self,
        banco: Database[Documento],
        *,
        relogio: Relogio = agora_utc,
        mensagem: MensagemRecebida | None = None,
    ) -> None:
        self.banco = banco
        self._relogio = relogio
        self._mensagem = mensagem
        self._mensagem_gravada = False
        self._sessao: ClientSession | None = None
        self._agregados: list[tuple[AggregateRoot, str | None]] = []
        self._eventos_avulsos: list[IntegrationEvent] = []

    @property
    def sessao(self) -> ClientSession | None:
        """Sessao da transacao corrente; ``None`` fora de ``executar``."""
        return self._sessao

    def executar[T](self, trabalho: Callable[[], T]) -> T:
        """Roda ``trabalho`` numa transacao e devolve o resultado dele.

        Conflito de escrita (``WriteConflict``) reexecuta o trabalho inteiro,
        do zero: ele precisa reler o que usa, sem efeito fora do banco (I/O
        com o provedor fica fora da transacao). Sem aninhamento.
        """
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
            self._gravar_mensagem_processada(sessao)
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
        self._mensagem_gravada = self._mensagem is not None
        for agregado, _colecao in self._agregados:
            agregado.limpar_eventos()
        self._agregados.clear()
        return resultado

    def concluir_mensagem(self) -> None:
        """Garante ``mensagens_processadas`` quando o comando nao abriu
        transacao (ex.: o original atrasado, descartado sem efeito)."""
        if self._mensagem is not None and not self._mensagem_gravada:
            self.executar(lambda: None)

    def registrar(self, agregado: AggregateRoot, colecao: str | None = None) -> None:
        """Chamado pelos repositorios ao salvar: os eventos vao para a outbox."""
        self._exigir_transacao()
        if all(registrado is not agregado for registrado, _ in self._agregados):
            self._agregados.append((agregado, colecao))

    def gravar(
        self, colecao: str, agregado: AggregateRoot, documento: Documento
    ) -> None:
        """Upsert do documento do agregado nesta transacao (os eventos dele vao
        para a outbox); criado durante um comando, guarda ``aberto_por``.

        ``$set`` e nao ``replace``: o ``aberto_por`` gravado na criacao
        (``$setOnInsert``) sobrevive as gravacoes seguintes.
        """
        self.registrar(agregado, colecao)
        atualizacao: Documento = {
            "$set": {
                chave: valor for chave, valor in documento.items() if chave != "_id"
            }
        }
        if self._mensagem is not None:
            atualizacao["$setOnInsert"] = {
                "aberto_por": {"mensagem_id": self._mensagem.id, **contexto_atual()}
            }
        self.banco[colecao].update_one(
            {"_id": agregado.id}, atualizacao, upsert=True, session=self._sessao
        )

    def registrar_evento(self, evento: IntegrationEvent) -> None:
        """Evento sem agregado alterado (ex.: o desfecho republicado)."""
        self._exigir_transacao()
        self._eventos_avulsos.append(evento)

    def _exigir_transacao(self) -> None:
        if self._sessao is None:
            msg = "Escrita fora de uma transacao: use UnitOfWork.executar"
            raise RuntimeError(msg)

    def _causa_corrente(self) -> _Causa:
        mensagem_id = None if self._mensagem is None else self._mensagem.id
        return _Causa(mensagem_id, contexto_atual())

    def _causa_de(
        self, agregado: AggregateRoot, colecao: str | None, sessao: ClientSession
    ) -> _Causa:
        # Resposta a um comando: o comando em processamento. Sem comando, o que
        # abriu o registro (gravado na criacao), para a saga seguir num trace so.
        if self._mensagem is not None or colecao is None:
            return self._causa_corrente()
        doc = self.banco[colecao].find_one(
            {"_id": agregado.id}, {"aberto_por": 1}, session=sessao
        )
        aberto_por = (doc or {}).get("aberto_por")
        if not aberto_por:
            return self._causa_corrente()
        return _Causa(
            aberto_por["mensagem_id"],
            {
                chave: aberto_por[chave]
                for chave in _CONTEXTO_W3C
                if aberto_por.get(chave)
            },
        )

    def _gravar_outbox(self, sessao: ClientSession) -> None:
        agora = self._relogio()
        documentos: list[Documento] = []
        for agregado, colecao in self._agregados:
            eventos = agregado.coletar_eventos()
            if eventos:
                causa = self._causa_de(agregado, colecao, sessao)
                documentos += [_documento_da_outbox(e, causa, agora) for e in eventos]
        if self._eventos_avulsos:
            causa = self._causa_corrente()
            documentos += [
                _documento_da_outbox(e, causa, agora) for e in self._eventos_avulsos
            ]
        if documentos:
            self.banco[COLECAO_OUTBOX].insert_many(documentos, session=sessao)

    def _gravar_mensagem_processada(self, sessao: ClientSession) -> None:
        # Upsert: o mesmo id de novo (reentrega) nao falha nem muda nada; a
        # transacao do efeito leva o registro junto (RFC-004, secao 5.4).
        mensagem = self._mensagem
        if mensagem is None or self._mensagem_gravada:
            return
        self.banco[COLECAO_PROCESSADAS].update_one(
            {"_id": mensagem.id},
            {
                "$setOnInsert": {
                    "tipo": mensagem.tipo,
                    "correlation_id": mensagem.correlation_id,
                    "processada_em": self._relogio(),
                }
            },
            upsert=True,
            session=sessao,
        )


def _documento_da_outbox(
    evento: IntegrationEvent, causa: _Causa, agora: datetime
) -> Documento:
    # UUIDv7: ordenado pelo tempo e monotonico no mesmo milissegundo, o relay
    # publica em ordem de ``_id`` e eventos da mesma transacao saem na ordem em
    # que o dominio os registrou.
    mensagem_id = uuid7()
    envelope = para_envelope(
        evento,
        mensagem_id=mensagem_id,
        causation_id=causa.mensagem_id,
        ocorrido_em=agora,
    )
    # Fora do contrato e defeito deste servico: aborta a transacao (500 na API).
    contratos.validar(envelope)
    return {
        "_id": mensagem_id,
        "tipo": evento.tipo,
        "status": "pendente",
        "tentativas": 0,
        "criado_em": agora,
        "proxima_tentativa_em": agora,
        "exchange": contratos.EXCHANGE_EVENTOS,
        "routing_key": contratos.routing_key_do_evento(evento.tipo),
        **causa.contexto,
        "envelope": envelope,
    }


ESQUEMA_OUTBOX: Documento = {
    "bsonType": "object",
    "required": [
        "tipo",
        "status",
        "tentativas",
        "criado_em",
        "proxima_tentativa_em",
        "exchange",
        "routing_key",
        "envelope",
    ],
    "properties": {
        "tipo": {"bsonType": "string"},
        "status": {"enum": ["pendente", "em_entrega", "entregue", "dead"]},
        "tentativas": {"bsonType": ["int", "long"], "minimum": 0},
        "criado_em": {"bsonType": "date"},
        "proxima_tentativa_em": {"bsonType": "date"},
        "exchange": {"bsonType": "string"},
        "routing_key": {"bsonType": "string"},
        "traceparent": {"bsonType": "string"},
        "tracestate": {"bsonType": "string"},
        "entregue_em": {"bsonType": "date"},
        "envelope": {"bsonType": "object"},
    },
}

ESQUEMA_PROCESSADAS: Documento = {
    "bsonType": "object",
    "required": ["tipo", "correlation_id", "processada_em"],
    "properties": {
        "tipo": {"bsonType": "string"},
        "correlation_id": {"bsonType": "binData"},
        "processada_em": {"bsonType": "date"},
    },
}


def preparar_outbox(banco: Database[Documento]) -> None:
    """Validadores e indices da outbox e de ``mensagens_processadas`` (init do
    banco, idempotente). Retencao por indice TTL, sem codigo de limpeza."""
    aplicar_validador(banco, COLECAO_OUTBOX, ESQUEMA_OUTBOX)
    outbox = banco[COLECAO_OUTBOX]
    # Claim do relay: pendentes (e leases vencidos) na ordem da proxima tentativa.
    outbox.create_index([("status", 1), ("proxima_tentativa_em", 1), ("_id", 1)])
    outbox.create_index(
        "entregue_em",
        expireAfterSeconds=int(RETENCAO_OUTBOX.total_seconds()),
        partialFilterExpression={"status": "entregue"},
    )
    aplicar_validador(banco, COLECAO_PROCESSADAS, ESQUEMA_PROCESSADAS)
    banco[COLECAO_PROCESSADAS].create_index(
        "processada_em", expireAfterSeconds=int(RETENCAO_PROCESSADAS.total_seconds())
    )
