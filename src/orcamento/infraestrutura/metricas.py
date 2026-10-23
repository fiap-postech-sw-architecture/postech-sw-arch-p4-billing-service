"""Metricas do orcamento (ADR-043)."""

from __future__ import annotations

import logging
from typing import Final

from prometheus_client import Histogram

from src.orcamento.dominio.orcamento import StatusOrcamento

_log = logging.getLogger(__name__)

# De 100 ms a 3 dias: a demonstracao decide em cerca de 1 s, e no uso real o
# orcamento vale 72 h e quem nao decide expira. O histogram_quantile interpola
# linearmente dentro do balde, entao com o primeiro balde em 60 s toda decisao
# rapida saia com p90 de 54 s (0,9 x 60), inclusive a unica de uma recusa.
TEMPO_DE_DECISAO = Histogram(
    "pytstop_orcamentos_tempo_decisao_segundos",
    "Tempo entre a geracao do orcamento e a decisao do cliente (link ou "
    "atendente), por decisao.",
    ["decisao"],
    buckets=(
        0.1,
        0.5,
        1,
        2,
        5,
        15,
        30,
        60,
        300,
        900,
        3600,
        14400,
        43200,
        86400,
        172800,
        259200,
    ),
)
# So estas duas decisoes existem; outra e defeito: vai para o log, nao vira serie
# nova e nao derruba o caso de uso (a decisao ja comitou).
_DECISOES: Final = {
    StatusOrcamento.APROVADO: "aprovado",
    StatusOrcamento.RECUSADO: "recusado",
}
# Series com zero desde o boot: o painel nao depende da 1a decisao.
for _rotulo in _DECISOES.values():
    TEMPO_DE_DECISAO.labels(decisao=_rotulo)


class MetricasDeOrcamentoPrometheus:
    """``MetricasDeOrcamento`` sobre o ``prometheus_client`` (``/metrics``)."""

    def decisao_registrada(self, decisao: StatusOrcamento, segundos: float) -> None:
        rotulo = _DECISOES.get(decisao)
        if rotulo is None:
            _log.error("decision_metric_skipped", extra={"status": decisao.value})
            return
        TEMPO_DE_DECISAO.labels(decisao=rotulo).observe(segundos)
