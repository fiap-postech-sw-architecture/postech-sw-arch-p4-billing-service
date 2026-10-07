"""Apoio dos testes de integracao: outbox, relogio, configuracao e mensageria."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pika
from pymongo import MongoClient, monitoring

from src.configuracao import Configuracao
from src.orcamento.aplicacao.link_decisao import LinkDeDecisao
from src.orcamento.interfaces.router_publico import PREFIXO as PREFIXO_DO_LINK
from src.pagamento.infraestrutura.simulado import (
    CAMINHO_CHECKOUT,
    GatewayPagamentoSimulado,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from pymongo.database import Database

    from src.compartilhado.infraestrutura.mensageria.consumidor import (
        ConsumidorDeComandos,
    )
    from src.pagamento.aplicacao.ports import CobrancaCriada, ItemCobranca
    from src.pagamento.dominio.cobranca import SituacaoNoProvedor
    from src.pagamento.dominio.estados import MotivoEstorno

URL_PUBLICA = "http://billing.teste"
SEGREDO_WEBHOOK = "segredo-do-webhook-de-teste"
# Valor de teste (ENVIRONMENT=test aceita qualquer segredo nao vazio).
SEGREDO_LINK = "segredo-do-link-de-teste-32-bytes!!"


LINK = LinkDeDecisao(segredo=SEGREDO_LINK, url_base=f"{URL_PUBLICA}{PREFIXO_DO_LINK}")


def token_do_link(orcamento_id: UUID, valido_ate: datetime | None) -> str:
    """Token do link de decisao que o ``OrcamentoGerado`` leva."""
    assert valido_ate is not None, "a lapide nao tem link"
    return LINK.gerar(orcamento_id, valido_ate).rsplit("/", 1)[1]


def token_do_checkout(checkout_url: str | None) -> str:
    """O ``?token=`` que o simulador pos no ``checkout_url``."""
    assert checkout_url is not None, "a lapide nao tem checkout"
    [token] = parse_qs(urlsplit(checkout_url).query)["token"]
    return token


def eventos_do_outbox(
    banco: Database[dict[str, Any]], tipo: str | None = None
) -> list[dict[str, Any]]:
    """Envelopes gravados na outbox, na ordem do relay (``_id`` UUIDv7)."""
    filtro = {"tipo": tipo} if tipo else {}
    return [doc["envelope"] for doc in banco["outbox"].find(filtro).sort("_id")]


class EscritasEspiadas(monitoring.CommandListener):
    """Guarda colecao, sessao e transacao de cada insert/update enviado."""

    def __init__(self) -> None:
        self.escritas: list[tuple[str, Any, Any, Any]] = []

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        if event.command_name in {"insert", "update"}:
            comando = event.command
            self.escritas.append(
                (
                    comando[event.command_name],
                    comando.get("lsid"),
                    comando.get("txnNumber"),
                    comando.get("autocommit"),
                )
            )

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        return None

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        return None


class ComandosEspiados(EscritasEspiadas):
    """Guarda tambem o nome e o documento de cada comando enviado."""

    def __init__(self) -> None:
        super().__init__()
        self.comandos: list[tuple[str, dict[str, Any]]] = []

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        super().started(event)
        self.comandos.append((event.command_name, dict(event.command)))


@contextmanager
def cliente_espiado(
    mongo_uri: str,
) -> Iterator[tuple[MongoClient[dict[str, Any]], ComandosEspiados]]:
    """Cliente do mesmo banco de teste que registra todo comando enviado."""
    espia = ComandosEspiados()
    cliente: MongoClient[dict[str, Any]] = MongoClient(
        mongo_uri,
        uuidRepresentation="standard",
        tz_aware=True,
        event_listeners=[espia],
    )
    try:
        yield cliente, espia
    finally:
        cliente.close()


# O RelogioFixo grava no passado (06/10/2026): com os indices TTL reais, o
# monitor do mongod apagaria as linhas no meio do teste depois de 7 ou 30 dias
# de calendario. A suite roda sem eles; test_outbox confere a especificacao
# deles num banco proprio.
INDICES_TTL = {
    "outbox": ("entregue_em_1", "morta_em_1"),
    "mensagens_processadas": ("processada_em_1",),
}


def sem_indices_ttl(banco: Database[dict[str, Any]]) -> None:
    for colecao, indices in INDICES_TTL.items():
        for indice in indices:
            banco[colecao].drop_index(indice)


class RelogioFixo:
    """Relogio controlavel; ``avancar`` move o tempo dos casos de uso."""

    def __init__(self, agora: datetime | None = None) -> None:
        self.agora = agora or datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.agora

    def avancar(self, **delta: float) -> datetime:
        self.agora += timedelta(**delta)
        return self.agora


def configuracao(**extra: str) -> Configuracao:
    return Configuracao.do_ambiente(
        {
            "ENVIRONMENT": "test",
            "MP_MODE": "simulado",
            "BILLING_PUBLIC_URL": URL_PUBLICA,
            "JWKS_URL": "http://os.teste/.well-known/jwks.json",
            "ORCAMENTO_LINK_SECRET": SEGREDO_LINK,
            "MP_WEBHOOK_SECRET": SEGREDO_WEBHOOK,
            **extra,
        }
    )


class MetricasEspia:
    """``MetricasDePagamento`` que guarda as chamadas (sem Prometheus)."""

    def __init__(self) -> None:
        self.estornos: list[MotivoEstorno] = []
        self.estornos_automaticos_recusados = 0
        self.cancelamentos_recusados = 0

    def estorno_concluido(self, motivo: MotivoEstorno) -> None:
        self.estornos.append(motivo)

    def estorno_automatico_falhou(self) -> None:
        self.estornos_automaticos_recusados += 1

    def cancelamento_de_cobranca_recusado(self) -> None:
        self.cancelamentos_recusados += 1


class GatewayRoteirizado(GatewayPagamentoSimulado):
    """Simulador real com ganchos: falhas programadas, respostas fixas e espias."""

    def __init__(self) -> None:
        super().__init__(
            url_checkout=f"{URL_PUBLICA}{CAMINHO_CHECKOUT}", segredo=SEGREDO_LINK
        )
        self.cobrancas: list[UUID] = []
        self.cancelamentos: list[str] = []
        self.estornos: list[tuple[str, str]] = []
        self.erro_na_cobranca: Exception | None = None
        self.erro_no_cancelamento: Exception | None = None
        self.erro_no_estorno: Exception | None = None
        self.respostas: dict[str, SituacaoNoProvedor | None] = {}

    def criar_cobranca(
        self, *, pagamento_id: UUID, itens: Sequence[ItemCobranca], expira_em: datetime
    ) -> CobrancaCriada:
        self.cobrancas.append(pagamento_id)
        if self.erro_na_cobranca:
            raise self.erro_na_cobranca
        return super().criar_cobranca(
            pagamento_id=pagamento_id, itens=itens, expira_em=expira_em
        )

    def cancelar_cobranca(self, referencia_preferencia: str) -> None:
        self.cancelamentos.append(referencia_preferencia)
        if self.erro_no_cancelamento:
            raise self.erro_no_cancelamento
        super().cancelar_cobranca(referencia_preferencia)

    def consultar_pagamento(self, referencia: str) -> SituacaoNoProvedor | None:
        if referencia in self.respostas:
            return self.respostas[referencia]
        return super().consultar_pagamento(referencia)

    def estornar(self, referencia: str, *, chave_idempotencia: str) -> None:
        self.estornos.append((referencia, chave_idempotencia))
        if self.erro_no_estorno:
            raise self.erro_no_estorno
        super().estornar(referencia, chave_idempotencia=chave_idempotencia)


# --- Mensageria: comandos do OS e canal AMQP de mentira ----------------------


class CanalDeTeste:
    """``Canal`` do consumidor que so anota publicacoes, acks e rejeicoes."""

    def __init__(self, erro_ao_publicar: Exception | None = None) -> None:
        self.publicadas: list[tuple[str, str, bytes, pika.BasicProperties]] = []
        self.confirmadas: list[int] = []
        self.rejeitadas: list[int] = []
        self.erro_ao_publicar = erro_ao_publicar

    def publicar(
        self,
        exchange: str,
        routing_key: str,
        corpo: bytes,
        propriedades: pika.BasicProperties,
    ) -> None:
        if self.erro_ao_publicar is not None:
            raise self.erro_ao_publicar
        self.publicadas.append((exchange, routing_key, corpo, propriedades))

    def confirmar(self, entrega: int) -> None:
        self.confirmadas.append(entrega)

    def rejeitar(self, entrega: int) -> None:
        self.rejeitadas.append(entrega)

    def aguardar(self, segundos: float) -> None:
        return None


class PublicadorFalso:
    """Guarda cada publicacao; pode segurar a chamada ou falhar depois dela."""

    def __init__(
        self,
        erro: Exception | None = None,
        segura: threading.Event | None = None,
    ) -> None:
        self.publicadas: list[tuple[str, str, bytes, pika.BasicProperties]] = []
        self.erro = erro
        self.segura = segura
        self.entrou = threading.Event()

    def publicar(
        self,
        exchange: str,
        routing_key: str,
        corpo: bytes,
        propriedades: pika.BasicProperties,
    ) -> None:
        self.entrou.set()
        if self.segura is not None:
            assert self.segura.wait(10)
        self.publicadas.append((exchange, routing_key, corpo, propriedades))
        if self.erro is not None:
            raise self.erro

    @property
    def ids(self) -> list[str]:
        return [propriedades.message_id for *_, propriedades in self.publicadas]


def comando(
    tipo: str,
    dados: dict[str, Any],
    *,
    mensagem_id: UUID | None = None,
) -> dict[str, Any]:
    """Envelope de um comando do OS (``correlation_id`` = ``ordem_id``)."""
    return {
        "id": str(mensagem_id or uuid4()),
        "tipo": tipo,
        "versao": 1,
        "origem": "os-service",
        "correlation_id": dados["ordem_id"],
        "causation_id": str(uuid4()),
        "ocorrido_em": "2026-10-06T12:00:00Z",
        "dados": dados,
    }


def propriedades(
    envelope: dict[str, Any],
    *,
    user_id: str | None = "os",
    cabecalhos: dict[str, Any] | None = None,
) -> pika.BasicProperties:
    return pika.BasicProperties(
        message_id=envelope["id"],
        correlation_id=envelope["correlation_id"],
        type=envelope["tipo"],
        user_id=user_id,
        content_type="application/json",
        delivery_mode=2,
        headers=cabecalhos or {},
    )


def entregar(
    consumidor: ConsumidorDeComandos,
    canal: CanalDeTeste,
    envelope: dict[str, Any],
    **opcoes: Any,
) -> str:
    """Uma entrega da fila ao consumidor (tag sequencial no canal)."""
    tag = len(canal.confirmadas) + len(canal.rejeitadas) + 1
    corpo = json.dumps(envelope).encode()
    return consumidor.tratar(canal, tag, propriedades(envelope, **opcoes), corpo)


# --- RabbitMQ de teste: as definitions do platform com TTL de retry curto ---

RAIZ_DO_REPO = Path(__file__).resolve().parents[2]
SENHA_DE_TESTE = "senha-de-teste-do-broker"  # gitleaks:allow (container de teste)
TTL_DE_RETRY_NO_TESTE_MS = 100
FILAS_DO_BILLING = (
    "billing.comandos",
    *(
        f"billing.comandos.retry.{nivel}"
        for nivel in ("1s", "5s", "15s", "60s", "300s")
    ),
    "billing.comandos.dlq",
    "os.eventos",
)


def definicoes_de_teste() -> dict[str, Any]:
    """``contratos/rabbitmq/definitions.json`` com TTL curto nas filas de retry,
    mais os usuarios dos servicos e as permissoes de ``permissoes.json``."""
    pasta = RAIZ_DO_REPO / "contratos" / "rabbitmq"
    definicoes: dict[str, Any] = json.loads((pasta / "definitions.json").read_text())
    permissoes: dict[str, Any] = json.loads((pasta / "permissoes.json").read_text())
    for fila in definicoes["queues"]:
        if "x-message-ttl" in fila["arguments"]:
            fila["arguments"]["x-message-ttl"] = TTL_DE_RETRY_NO_TESTE_MS
    definicoes["users"] = [
        {"name": usuario, "password": SENHA_DE_TESTE, "tags": []}
        for usuario in ("os", "billing", "execucao")
    ]
    definicoes["permissions"] = permissoes["permissions"]
    definicoes["topic_permissions"] = permissoes["topic_permissions"]
    return definicoes


@dataclass(frozen=True, slots=True)
class BrokerDeTeste:
    host: str
    porta: int
    api: str
    container: Any

    def url(self, usuario: str) -> str:
        senha = SENHA_DE_TESTE if usuario != "admin" else SENHA_DO_ADMIN
        return f"amqp://{usuario}:{senha}@{self.host}:{self.porta}/%2F"

    def conectar(self, usuario: str = "admin") -> pika.BlockingConnection:
        return pika.BlockingConnection(pika.URLParameters(self.url(usuario)))


SENHA_DO_ADMIN = "senha-do-admin-de-teste"  # gitleaks:allow (container de teste)
