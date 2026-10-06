"""Metricas do pagamento (catalogo da RFC-004, secao 9)."""

from __future__ import annotations

from prometheus_client import Counter

from src.pagamento.dominio.estados import MotivoEstorno

PAGAMENTOS_ESTORNADOS = Counter(
    "pytstop_pagamentos_estornados",
    "Estornos concluidos no provedor, por motivo (compensacao ou "
    "pagamento_apos_encerramento).",
    ["motivo"],
)
ESTORNOS_AUTOMATICOS_RECUSADOS = Counter(
    "pytstop_estornos_automaticos_recusados",
    "Estornos automaticos recusados pelo provedor: devolucao fica para o operador.",
)
CANCELAMENTOS_DE_COBRANCA_RECUSADOS = Counter(
    "pytstop_cancelamentos_de_cobranca_recusados",
    "Checkouts que o provedor recusou fechar na compensacao: o pagamento "
    "cancela assim mesmo e a aprovacao tardia e estornada.",
)
# Series com zero desde o boot: o painel e o alerta nao dependem do 1o estorno.
for _motivo in MotivoEstorno:
    PAGAMENTOS_ESTORNADOS.labels(motivo=_motivo.value)


class MetricasPrometheus:
    """``MetricasDePagamento`` sobre o ``prometheus_client`` (``/metrics``)."""

    def estorno_concluido(self, motivo: MotivoEstorno) -> None:
        PAGAMENTOS_ESTORNADOS.labels(motivo=motivo.value).inc()

    def estorno_automatico_falhou(self) -> None:
        ESTORNOS_AUTOMATICOS_RECUSADOS.inc()

    def cancelamento_de_cobranca_recusado(self) -> None:
        CANCELAMENTOS_DE_COBRANCA_RECUSADOS.inc()
