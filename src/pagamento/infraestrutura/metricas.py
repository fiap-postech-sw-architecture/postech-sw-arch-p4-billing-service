"""Metricas do pagamento (catalogo da RFC-004, secao 9)."""

from __future__ import annotations

from typing import Final

from prometheus_client import Counter

from src.pagamento.dominio.estados import MotivoEstorno, StatusPagamento

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
# Dinheiro em centavos inteiros: o Prometheus guarda float64, e o painel divide
# por 100 (a regra de nunca usar float para dinheiro vale ate o ultimo inteiro).
PAGAMENTOS_VALOR = Counter(
    "pytstop_pagamentos_valor_centavos",
    "Valor dos pagamentos que entraram em CONFIRMADO e dos confirmados que "
    "foram a ESTORNADO, em centavos.",
    ["estado"],
)
# So estes dois estados contam valor; outro e defeito (KeyError), nao serie nova.
_ESTADOS: Final = {
    StatusPagamento.CONFIRMADO: "confirmado",
    StatusPagamento.ESTORNADO: "estornado",
}
# Series com zero desde o boot: o painel e o alerta nao dependem do 1o evento.
for _motivo in MotivoEstorno:
    PAGAMENTOS_ESTORNADOS.labels(motivo=_motivo.value)
for _rotulo in _ESTADOS.values():
    PAGAMENTOS_VALOR.labels(estado=_rotulo)


class MetricasPrometheus:
    """``MetricasDePagamento`` sobre o ``prometheus_client`` (``/metrics``)."""

    def estorno_concluido(self, motivo: MotivoEstorno) -> None:
        PAGAMENTOS_ESTORNADOS.labels(motivo=motivo.value).inc()

    def estorno_automatico_falhou(self) -> None:
        ESTORNOS_AUTOMATICOS_RECUSADOS.inc()

    def cancelamento_de_cobranca_recusado(self) -> None:
        CANCELAMENTOS_DE_COBRANCA_RECUSADOS.inc()

    def valor_registrado(self, estado: StatusPagamento, centavos: int) -> None:
        PAGAMENTOS_VALOR.labels(estado=_ESTADOS[estado]).inc(centavos)
