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
_PENDENTES: Final = {"status": {"$in": ["pendente", "em_entrega"]}}

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


class ColetorDaOutbox(Collector):
    """``outbox_pendentes`` (pendente ou em entrega), ``outbox_dead`` e
    ``pytstop_outbox_mais_antiga_segundos`` (idade da pendente mais antiga, zero
    sem pendente).

    O relay e a API registram o coletor: com o relay parado a API segue
    exportando a idade, que e o que mantem o alerta de outbox parada de pe. Banco
    fora do ar nao derruba o scrape: os gauges somem ate ele voltar.
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
        """Sem nomes a declarar: o registro nao consulta o banco para descobri-los
        (o boot da API nao espera o MongoDB)."""
        return ()

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            pendentes = self._outbox.count_documents(_PENDENTES)
            mortas = self._outbox.count_documents({"status": "dead"})
            mais_antiga = self._outbox.find_one(
                _PENDENTES, {"criado_em": 1}, sort=[("criado_em", 1)]
            )
        except PyMongoError:
            _log.warning("outbox_metrics_unavailable")
            return
        yield GaugeMetricFamily(
            "outbox_pendentes",
            "Mensagens da outbox ainda nao confirmadas pelo broker.",
            value=pendentes,
        )
        yield GaugeMetricFamily(
            "outbox_dead",
            "Mensagens da outbox que esgotaram as tentativas (status dead).",
            value=mortas,
        )
        yield GaugeMetricFamily(
            "pytstop_outbox_mais_antiga_segundos",
            "Idade da linha pendente mais antiga da outbox (0 sem pendente).",
            value=0.0
            if mais_antiga is None
            else max(0.0, (self._relogio() - mais_antiga["criado_em"]).total_seconds()),
        )
