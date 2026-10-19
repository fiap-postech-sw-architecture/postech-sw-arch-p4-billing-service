"""Metricas da mensageria (ADR-043): contadores com prefixo ``pytstop_`` e os
gauges herdados do relay do p3 com o nome original (``outbox_pendentes``,
``outbox_dead``), calculados por consulta a cada scrape, mais a idade da
linha pendente mais antiga da outbox.

Toda serie de rotulo fechado nasce com zero (``preinicializar_*``): sem isso o
``increase()`` do Prometheus perde o primeiro evento de cada serie.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

import pymongo
from prometheus_client import Counter
from prometheus_client.metrics_core import GaugeMetricFamily
from prometheus_client.registry import Collector
from pymongo.errors import PyMongoError

from src.compartilhado.dominio.relogio import agora_utc

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from pymongo.database import Database

    from src.compartilhado.dominio.relogio import Relogio
    from src.compartilhado.infraestrutura.mongo import Documento

_log = logging.getLogger(__name__)

TIPO_DESCONHECIDO: Final = "desconhecido"
# O que o consumidor conta por mensagem (rotulo ``resultado``).
RESULTADOS_CONSUMIDOS: Final = ("processada", "duplicada", "ignorada", "retry", "dlq")
# Pendente ou em entrega: ainda nao confirmada pelo broker.
PENDENTES: Final = {"status": {"$in": ["pendente", "em_entrega"]}}
# A mais antiga: o indice (status, criado_em) a entrega sem ordenar em memoria.
MAIS_ANTIGA: Final = (("criado_em", 1),)
# Prazo das consultas de UMA raspagem, todas juntas: o Prometheus do platform
# espera 10 s, e o cliente espera ate 5 s so para achar o servidor. Com o banco
# fora, o ``/metrics`` responde em cerca de 2 s, sem as tres series.
PRAZO_DA_RASPAGEM_S: Final = 2.0

MENSAGENS_PUBLICADAS = Counter(
    "pytstop_mensagens_publicadas_total",
    "Mensagens da outbox confirmadas pelo broker, por tipo.",
    ["tipo"],
)
MENSAGENS_CONSUMIDAS = Counter(
    "pytstop_mensagens_consumidas_total",
    "Mensagens consumidas por tipo e resultado "
    "(processada, duplicada, ignorada, retry, dlq).",
    ["tipo", "resultado"],
)
# O rotulo e ``componente`` e nao ``processo``: o Prometheus ja poe ``processo``
# em todo alvo, e um rotulo da metrica com o mesmo nome viraria
# ``exported_processo`` (ADR-043).
RECONEXOES = Counter(
    "pytstop_reconexoes_ao_broker_total",
    "Esperas antes de reconectar ao RabbitMQ (conexao perdida ou recusada).",
    ["componente"],
)


def preinicializar_publicadas(tipos: Iterable[str]) -> None:
    """Uma serie em zero por tipo de evento que o relay publica."""
    for tipo in tipos:
        MENSAGENS_PUBLICADAS.labels(tipo=tipo)


def preinicializar_consumidas(tipos: Iterable[str]) -> None:
    """Uma serie em zero por tipo e resultado. O tipo ``desconhecido`` so tem o
    resultado ``dlq``: a mensagem sem tipo conhecido nao chega a um handler."""
    for tipo in tipos:
        for resultado in RESULTADOS_CONSUMIDOS:
            MENSAGENS_CONSUMIDAS.labels(tipo=tipo, resultado=resultado)
    MENSAGENS_CONSUMIDAS.labels(tipo=TIPO_DESCONHECIDO, resultado="dlq")


def _pendentes(valor: float | None = None) -> GaugeMetricFamily:
    return GaugeMetricFamily(
        "outbox_pendentes",
        "Mensagens da outbox ainda nao confirmadas pelo broker.",
        value=valor,
    )


def _mortas(valor: float | None = None) -> GaugeMetricFamily:
    return GaugeMetricFamily(
        "outbox_dead",
        "Mensagens da outbox que esgotaram as tentativas (status dead).",
        value=valor,
    )


def _idade(valor: float | None = None) -> GaugeMetricFamily:
    return GaugeMetricFamily(
        "pytstop_outbox_mais_antiga_segundos",
        "Idade da linha pendente mais antiga da outbox (0 sem pendente).",
        value=valor,
    )


class ColetorDaOutbox(Collector):
    """``outbox_pendentes`` (pendente ou em entrega), ``outbox_dead`` e
    ``pytstop_outbox_mais_antiga_segundos`` (idade da pendente mais antiga, zero
    sem pendente).

    O relay e a API registram o coletor: com o relay parado a API segue
    exportando a idade, que e o que mantem o alerta de outbox parada de pe. As
    tres consultas dividem o prazo de ``PRAZO_DA_RASPAGEM_S``; banco fora do ar,
    lento ou sem o indice nao derruba nem atrasa o scrape: os gauges somem ate
    ele voltar (nunca viram zero).
    """

    def __init__(
        self,
        outbox_colecao: str,
        banco: Database[Documento],
        *,
        relogio: Relogio = agora_utc,
    ) -> None:
        self._outbox = banco[outbox_colecao]
        self._relogio = relogio

    def describe(self) -> Iterable[GaugeMetricFamily]:
        """Os nomes, sem consultar o banco (o registro roda antes de o boot da API
        terminar): o registro recusa um segundo coletor com os mesmos nomes."""
        return (_pendentes(), _mortas(), _idade())

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            with pymongo.timeout(PRAZO_DA_RASPAGEM_S):
                pendentes = self._outbox.count_documents(PENDENTES)
                mortas = self._outbox.count_documents({"status": "dead"})
                mais_antiga = self._outbox.find_one(
                    PENDENTES, {"criado_em": 1}, sort=list(MAIS_ANTIGA)
                )
        except PyMongoError as exc:
            _log.warning(
                "outbox_metrics_unavailable",
                extra={"tipo_do_erro": type(exc).__name__},
            )
            return
        yield _pendentes(pendentes)
        yield _mortas(mortas)
        yield _idade(
            0.0
            if mais_antiga is None
            else max(0.0, (self._relogio() - mais_antiga["criado_em"]).total_seconds())
        )
