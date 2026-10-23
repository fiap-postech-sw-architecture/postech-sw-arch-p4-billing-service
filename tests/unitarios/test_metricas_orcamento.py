from __future__ import annotations

import logging
import math

import pytest
from prometheus_client import REGISTRY, CollectorRegistry, Histogram

from src.orcamento.dominio.orcamento import StatusOrcamento
from src.orcamento.infraestrutura.metricas import MetricasDeOrcamentoPrometheus
from tests.conftest import valor_da_metrica

NOME = "pytstop_orcamentos_tempo_decisao_segundos"
# 100 ms a 3 dias: a demonstracao decide em cerca de 1 s e o orcamento vale 72 h.
LIMITES = (
    "0.1",
    "0.5",
    "1.0",
    "2.0",
    "5.0",
    "15.0",
    "30.0",
    "60.0",
    "300.0",
    "900.0",
    "3600.0",
    "14400.0",
    "43200.0",
    "86400.0",
)
LIMITES_ALEM = ("172800.0", "259200.0", "+Inf")
# Os baldes de antes: o primeiro, de 60 s, engolia toda decisao rapida.
BALDES_ANTIGOS = (60, 300, 900, 3600, 14400, 43200, 86400, 172800, 259200)


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
    for le in LIMITES[: LIMITES.index("300.0")]:
        assert amostra("bucket", decisao="aprovado", le=le) == antes["aprovado", le]
    for le in (*LIMITES[LIMITES.index("300.0") :], *LIMITES_ALEM):
        assert amostra("bucket", decisao="aprovado", le=le) == antes["aprovado", le] + 1
    # A outra decisao nao mexe.
    assert amostra("bucket", decisao="recusado", le="+Inf") == antes["recusado", "+Inf"]


def baldes_finitos() -> list[float]:
    """Os limites do histograma de producao, lidos do ``/metrics`` (sem o ``+Inf``)."""
    return sorted(
        float(amostra_.labels["le"])
        for familia in REGISTRY.collect()
        if familia.name == NOME
        for amostra_ in familia.samples
        if amostra_.name.endswith("_bucket")
        and amostra_.labels["decisao"] == "aprovado"
        and amostra_.labels["le"] != "+Inf"
    )


def test_baldes_vao_de_cem_milissegundos_a_tres_dias() -> None:
    les = {
        amostra_.labels["le"]
        for familia in REGISTRY.collect()
        if familia.name == NOME
        for amostra_ in familia.samples
        if amostra_.name.endswith("_bucket")
    }
    assert les == {*LIMITES, *LIMITES_ALEM}


def p90(baldes: tuple[float, ...] | list[float], observacoes: list[float]) -> float:
    """O ``histogram_quantile(0.9, ...)`` do Prometheus sobre um histograma novo.

    Acha o primeiro balde cujo total cumulativo alcanca 90% das observacoes e
    interpola linearmente entre o limite de baixo (0 no primeiro balde) e o de cima.
    Acima do ultimo balde finito devolve esse limite.
    """
    registro = CollectorRegistry()
    histograma = Histogram("decisao", "teste", buckets=baldes, registry=registro)
    for segundos in observacoes:
        histograma.observe(segundos)
    cumulativos = sorted(
        (float(amostra_.labels["le"]), amostra_.value)
        for familia in registro.collect()
        for amostra_ in familia.samples
        if amostra_.name == "decisao_bucket"
    )
    posto = 0.9 * cumulativos[-1][1]
    limite_de_baixo, contagem_de_baixo = 0.0, 0.0
    for limite, contagem in cumulativos:
        if contagem >= posto:
            if math.isinf(limite):
                return limite_de_baixo
            alcance = (posto - contagem_de_baixo) / (contagem - contagem_de_baixo)
            return limite_de_baixo + (limite - limite_de_baixo) * alcance
        limite_de_baixo, contagem_de_baixo = limite, contagem
    raise AssertionError("o balde +Inf sempre alcanca o posto")  # pragma: no cover


# A demonstracao aprova pelo link cerca de 1 s depois de gerar o orcamento.
DECISOES_DA_DEMONSTRACAO = [0.95 + 0.01 * i for i in range(20)]


def test_o_balde_de_60_s_dava_p90_de_54_s_a_decisao_de_um_segundo() -> None:
    """Controle: reproduz o numero do painel (0,9 x 60 s) com os baldes de antes."""
    assert p90(BALDES_ANTIGOS, DECISOES_DA_DEMONSTRACAO) == pytest.approx(54.0)
    assert p90(BALDES_ANTIGOS, [1.05]) == pytest.approx(54.0)


@pytest.mark.parametrize(
    "observacoes",
    [DECISOES_DA_DEMONSTRACAO, [1.05], [0.3]],
    ids=["vinte decisoes", "uma decisao", "decisao de 300 ms"],
)
def test_decisao_rapida_nao_sai_com_percentil_de_um_minuto(
    observacoes: list[float],
) -> None:
    assert p90(baldes_finitos(), observacoes) <= 2.0


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
