from __future__ import annotations

import logging

import pytest
from prometheus_client import REGISTRY

from src.pagamento.dominio.estados import MotivoEstorno, StatusPagamento
from src.pagamento.infraestrutura.metricas import MetricasPrometheus
from tests.conftest import valor_da_metrica


def test_estornos_por_motivo_e_recusas_do_provedor() -> None:
    metricas = MetricasPrometheus()
    nome = "pytstop_pagamentos_estornados_total"
    antes = {m: valor_da_metrica(nome, motivo=m.value) for m in MotivoEstorno}
    recusados = valor_da_metrica("pytstop_estornos_automaticos_recusados_total")
    nao_fechados = valor_da_metrica("pytstop_cancelamentos_de_cobranca_recusados_total")

    metricas.estorno_concluido(MotivoEstorno.COMPENSACAO)
    metricas.estorno_automatico_falhou()
    metricas.cancelamento_de_cobranca_recusado()

    assert (
        valor_da_metrica(nome, motivo="compensacao")
        == antes[MotivoEstorno.COMPENSACAO] + 1
    )
    assert (
        valor_da_metrica(nome, motivo="pagamento_apos_encerramento")
        == (antes[MotivoEstorno.PAGAMENTO_APOS_ENCERRAMENTO])
    )
    assert (
        valor_da_metrica("pytstop_estornos_automaticos_recusados_total")
        == recusados + 1
    )
    assert (
        valor_da_metrica("pytstop_cancelamentos_de_cobranca_recusados_total")
        == nao_fechados + 1
    )


def test_valor_confirmado_e_estornado_em_centavos_inteiros() -> None:
    metricas = MetricasPrometheus()
    nome = "pytstop_pagamentos_valor_centavos_total"
    confirmado = valor_da_metrica(nome, estado="confirmado")
    estornado = valor_da_metrica(nome, estado="estornado")

    metricas.valor_registrado(StatusPagamento.CONFIRMADO, 33500)
    metricas.valor_registrado(StatusPagamento.CONFIRMADO, 1)
    metricas.valor_registrado(StatusPagamento.ESTORNADO, 33500)

    assert valor_da_metrica(nome, estado="confirmado") == confirmado + 33501
    assert valor_da_metrica(nome, estado="estornado") == estornado + 33500


@pytest.mark.parametrize(
    "estado",
    [
        s
        for s in StatusPagamento
        if s not in {StatusPagamento.CONFIRMADO, StatusPagamento.ESTORNADO}
    ],
)
def test_outro_estado_nao_vira_serie_nova_e_nao_derruba_o_caso_de_uso(
    estado: StatusPagamento, caplog: pytest.LogCaptureFixture
) -> None:
    """Rotulo fechado: so ``confirmado`` e ``estornado`` contam valor. O pagamento
    ja comitou quando a metrica conta: o defeito vai para o log, nao vira 500."""
    nome = "pytstop_pagamentos_valor_centavos"

    def series() -> set[tuple[str, ...]]:
        return {
            (amostra.name, *amostra.labels.values())
            for familia in REGISTRY.collect()
            if familia.name == nome
            for amostra in familia.samples
        }

    antes = series()

    with caplog.at_level(logging.ERROR):
        MetricasPrometheus().valor_registrado(estado, 100)

    assert series() == antes
    [registro] = [
        r for r in caplog.records if r.message == "payment_value_metric_skipped"
    ]
    assert registro.__dict__["status"] == estado.value
