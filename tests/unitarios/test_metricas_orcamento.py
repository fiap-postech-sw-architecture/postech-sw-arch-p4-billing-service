from __future__ import annotations

import logging

import pytest
from prometheus_client import REGISTRY

from src.orcamento.dominio.orcamento import StatusOrcamento
from src.orcamento.infraestrutura.metricas import MetricasDeOrcamentoPrometheus
from tests.conftest import valor_da_metrica

NOME = "pytstop_orcamentos_tempo_decisao_segundos"
# 1 min a 3 dias: o orcamento vale 72 h.
LIMITES = ("60.0", "300.0", "900.0", "3600.0", "14400.0", "43200.0", "86400.0")
LIMITES_ALEM = ("172800.0", "259200.0", "+Inf")


def amostra(sufixo: str, **rotulos: str) -> float:
    return valor_da_metrica(f"{NOME}_{sufixo}", **rotulos)


def test_decisao_entra_no_histograma_da_decisao() -> None:
    antes = {
        (d, le): amostra("bucket", decisao=d, le=le)
        for d in ("aprovado", "recusado")
        for le in (*LIMITES, *LIMITES_ALEM)
    }
    soma = amostra("sum", decisao="aprovado")

    # 2 min: cai no balde de 300 s em diante, nao no de 60 s.
    MetricasDeOrcamentoPrometheus().decisao_registrada(StatusOrcamento.APROVADO, 120.0)

    assert amostra("count", decisao="aprovado") >= 1
    assert amostra("sum", decisao="aprovado") == soma + 120.0
    assert amostra("bucket", decisao="aprovado", le="60.0") == antes["aprovado", "60.0"]
    for le in (*LIMITES[1:], *LIMITES_ALEM):
        assert amostra("bucket", decisao="aprovado", le=le) == antes["aprovado", le] + 1
    # A outra decisao nao mexe.
    assert amostra("bucket", decisao="recusado", le="+Inf") == antes["recusado", "+Inf"]


def test_baldes_vao_de_um_minuto_a_tres_dias() -> None:
    les = {
        amostra_.labels["le"]
        for familia in REGISTRY.collect()
        if familia.name == NOME
        for amostra_ in familia.samples
        if amostra_.name.endswith("_bucket")
    }
    assert les == {*LIMITES, *LIMITES_ALEM}


def test_recusa_conta_na_serie_recusado() -> None:
    antes = amostra("count", decisao="recusado")
    MetricasDeOrcamentoPrometheus().decisao_registrada(StatusOrcamento.RECUSADO, 5.0)
    assert amostra("count", decisao="recusado") == antes + 1


@pytest.mark.parametrize(
    "status",
    [
        s
        for s in StatusOrcamento
        if s not in {StatusOrcamento.APROVADO, StatusOrcamento.RECUSADO}
    ],
)
def test_outro_status_nao_vira_serie_nova_e_nao_derruba_o_caso_de_uso(
    status: StatusOrcamento, caplog: pytest.LogCaptureFixture
) -> None:
    """Rotulo fechado: expirado e cancelado nao sao decisao do cliente. A decisao
    ja comitou quando a metrica conta: o defeito vai para o log, nao vira 500."""

    def series() -> set[tuple[str, ...]]:
        return {
            (amostra.name, *amostra.labels.values())
            for familia in REGISTRY.collect()
            if familia.name == NOME
            for amostra in familia.samples
        }

    antes = series()

    with caplog.at_level(logging.ERROR):
        MetricasDeOrcamentoPrometheus().decisao_registrada(status, 1.0)

    assert series() == antes
    [registro] = [r for r in caplog.records if r.message == "decision_metric_skipped"]
    assert registro.__dict__["status"] == status.value
