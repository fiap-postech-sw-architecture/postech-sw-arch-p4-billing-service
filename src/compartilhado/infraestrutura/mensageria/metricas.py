"""Metricas da mensageria (ADR-043): contadores com prefixo ``pytstop_`` e os
gauges herdados do relay do p3 com o nome original (``outbox_pendentes``,
``outbox_dead``), calculados por consulta a cada scrape."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from prometheus_client import Counter
from prometheus_client.metrics_core import GaugeMetricFamily
from prometheus_client.registry import Collector
from pymongo.errors import PyMongoError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pymongo.database import Database

    from src.compartilhado.infraestrutura.mongo import Documento

_log = logging.getLogger(__name__)

TIPO_DESCONHECIDO: Final = "desconhecido"

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


class ColetorDaOutbox(Collector):
    """``outbox_pendentes`` (pendente ou em entrega) e ``outbox_dead``.

    Banco fora do ar nao derruba o scrape: os gauges somem ate ele voltar.
    """

    def __init__(self, outbox_colecao: str, banco: Database[Documento]) -> None:
        self._outbox = banco[outbox_colecao]

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            pendentes = self._outbox.count_documents(
                {"status": {"$in": ["pendente", "em_entrega"]}}
            )
            mortas = self._outbox.count_documents({"status": "dead"})
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
