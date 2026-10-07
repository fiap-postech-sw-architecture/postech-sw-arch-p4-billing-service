from __future__ import annotations

from prometheus_client import REGISTRY

from src.pagamento.dominio.estados import MotivoEstorno
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
