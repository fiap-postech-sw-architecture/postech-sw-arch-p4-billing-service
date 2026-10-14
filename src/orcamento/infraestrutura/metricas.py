"""Metricas do orcamento (catalogo da RFC-004, secao 9)."""

from __future__ import annotations

from typing import Final

from prometheus_client import Histogram

from src.orcamento.dominio.orcamento import StatusOrcamento

# De 1 minuto a 3 dias: o orcamento vale 72 h e quem nao decide expira.
TEMPO_DE_DECISAO = Histogram(
    "pytstop_orcamentos_tempo_decisao_segundos",
    "Tempo entre a geracao do orcamento e a decisao do cliente (link ou "
    "atendente), por decisao.",
    ["decisao"],
    buckets=(60, 300, 900, 3600, 14400, 43200, 86400, 172800, 259200),
)
# So estas duas decisoes existem; outra e defeito (KeyError), nao serie nova.
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
        TEMPO_DE_DECISAO.labels(decisao=_DECISOES[decisao]).observe(segundos)
