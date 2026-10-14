"""Metricas do orcamento (ADR-043)."""

from __future__ import annotations

import logging
from typing import Final

from prometheus_client import Histogram

from src.orcamento.dominio.orcamento import StatusOrcamento

_log = logging.getLogger(__name__)

# De 1 minuto a 3 dias: o orcamento vale 72 h e quem nao decide expira.
TEMPO_DE_DECISAO = Histogram(
    "pytstop_orcamentos_tempo_decisao_segundos",
    "Tempo entre a geracao do orcamento e a decisao do cliente (link ou "
    "atendente), por decisao.",
    ["decisao"],
    buckets=(60, 300, 900, 3600, 14400, 43200, 86400, 172800, 259200),
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
