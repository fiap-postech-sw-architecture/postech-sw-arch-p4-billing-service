from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from src.pagamento.dominio.estados import MotivoEstorno, StatusPagamento
from src.pagamento.infraestrutura.metricas import MetricasPrometheus


def amostra(nome: str, **rotulos: str) -> float:
    return REGISTRY.get_sample_value(nome, rotulos) or 0.0


def test_estornos_por_motivo_e_recusas_do_provedor() -> None:
    metricas = MetricasPrometheus()
    nome = "pytstop_pagamentos_estornados_total"
    antes = {m: amostra(nome, motivo=m.value) for m in MotivoEstorno}
    recusados = amostra("pytstop_estornos_automaticos_recusados_total")
    nao_fechados = amostra("pytstop_cancelamentos_de_cobranca_recusados_total")

    metricas.estorno_concluido(MotivoEstorno.COMPENSACAO)
    metricas.estorno_automatico_falhou()
    metricas.cancelamento_de_cobranca_recusado()

    assert amostra(nome, motivo="compensacao") == antes[MotivoEstorno.COMPENSACAO] + 1
    assert (
        amostra(nome, motivo="pagamento_apos_encerramento")
        == (antes[MotivoEstorno.PAGAMENTO_APOS_ENCERRAMENTO])
    )
    assert amostra("pytstop_estornos_automaticos_recusados_total") == recusados + 1
    assert (
        amostra("pytstop_cancelamentos_de_cobranca_recusados_total") == nao_fechados + 1
    )


def test_valor_confirmado_e_estornado_em_centavos_inteiros() -> None:
    metricas = MetricasPrometheus()
    nome = "pytstop_pagamentos_valor_centavos_total"
    confirmado = amostra(nome, estado="confirmado")
    estornado = amostra(nome, estado="estornado")

    metricas.valor_registrado(StatusPagamento.CONFIRMADO, 33500)
    metricas.valor_registrado(StatusPagamento.CONFIRMADO, 1)
    metricas.valor_registrado(StatusPagamento.ESTORNADO, 33500)

    assert amostra(nome, estado="confirmado") == confirmado + 33501
    assert amostra(nome, estado="estornado") == estornado + 33500


@pytest.mark.parametrize(
    "estado",
    [
        s
        for s in StatusPagamento
        if s not in {StatusPagamento.CONFIRMADO, StatusPagamento.ESTORNADO}
    ],
)
def test_outro_estado_nao_vira_serie_nova(estado: StatusPagamento) -> None:
    """Rotulo fechado: so ``confirmado`` e ``estornado`` contam valor."""
    with pytest.raises(KeyError):
        MetricasPrometheus().valor_registrado(estado, 100)
