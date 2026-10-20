"""O smoke exige familias e series do ``/metrics``.

A entrada dos testes e a exposicao que o ``prometheus_client`` escreve
(``# HELP``, ``# TYPE`` e os sufixos ``_total``, ``_created``, ``_bucket``,
``_sum`` e ``_count``), e nao uma linha por familia montada a mao. O catalogo do
smoke e conferido contra as familias que o codigo dos quatro processos define.
"""

from __future__ import annotations

import importlib
import io
import runpy
import sys
import urllib.request
from itertools import cycle
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from tests.conftest import carregar_script

if TYPE_CHECKING:
    from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke_metricas.py"
URL = "http://127.0.0.1/metrics"
FAMILIA_A_REMOVER = {
    "api": "pytstop_webhook_assinatura_invalida",
    "relay": "outbox_dead",
    "consumidor": "pytstop_cancelamentos_de_cobranca_recusados",
    "prazos": "pytstop_prazos_ultimo_ciclo_timestamp_seconds",
}


def exposicao_completa(modulo: ModuleType, processo: str) -> list[str]:
    """O ``/metrics`` do processo depois do fluxo: uma amostra de cada familia do
    catalogo, nos tres tipos (contador, histograma e gauge, para os sufixos), mais
    as series de rotulo que o smoke exige."""
    registro = CollectorRegistry()
    for nome, tipo in zip(
        modulo.FAMILIAS[processo], cycle((Counter, Histogram, Gauge)), strict=False
    ):
        tipo(nome, "ajuda", registry=registro)
    linhas = generate_latest(registro).decode().splitlines()
    return linhas + [
        s + "0.0" if s.endswith(" ") else s for s in modulo.SERIES[processo]
    ]


def sem_a_familia(modulo: ModuleType, linhas: list[str], ausente: str) -> list[str]:
    """Tira as amostras da familia e deixa o ``# HELP`` e o ``# TYPE`` dela: uma
    familia sem amostra e a que o ``increase()`` do Prometheus perde."""
    return [
        linha
        for linha in linhas
        if linha.startswith("#") or modulo.familia(linha) != ausente
    ]


def executar_smoke(
    monkeypatch: pytest.MonkeyPatch, processo: str, linhas: list[str]
) -> None:
    def abrir(url: str, *, timeout: int) -> io.BytesIO:
        assert (url, timeout) == (URL, 5)
        return io.BytesIO("\n".join(linhas).encode())

    monkeypatch.setattr(urllib.request, "urlopen", abrir)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), processo, URL])
    runpy.run_path(str(SCRIPT), run_name="__main__")


def test_a_entrada_dos_testes_e_a_exposicao_do_prometheus_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sem isto os testes abaixo voltariam a passar com linhas simples."""
    modulo = carregar_script("smoke_metricas", monkeypatch)
    linhas = exposicao_completa(modulo, "api")
    assert any(linha.startswith("# HELP ") for linha in linhas)
    assert any(linha.startswith("# TYPE ") for linha in linhas)
    nomes = {linha.split("{")[0].split()[0] for linha in linhas if linha[0] != "#"}
    for sufixo in ("_total", "_created", "_bucket", "_sum", "_count"):
        assert any(nome.endswith(sufixo) for nome in nomes)


@pytest.mark.parametrize("processo", list(FAMILIA_A_REMOVER))
def test_smoke_aceita_todas_as_familias_e_series(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], processo: str
) -> None:
    modulo = carregar_script("smoke_metricas", monkeypatch)
    executar_smoke(monkeypatch, processo, exposicao_completa(modulo, processo))
    saida = capsys.readouterr()
    assert f"metricas do {processo} ok" in saida.out
    assert saida.err == ""


@pytest.mark.parametrize("processo", list(FAMILIA_A_REMOVER))
def test_smoke_recusa_familia_obrigatoria_ausente(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], processo: str
) -> None:
    modulo = carregar_script("smoke_metricas", monkeypatch)
    ausente = FAMILIA_A_REMOVER[processo]
    # Estas familias nao tem uma serie em SERIES: so o gate de familia as guarda.
    linhas = sem_a_familia(modulo, exposicao_completa(modulo, processo), ausente)
    with pytest.raises(SystemExit) as erro:
        executar_smoke(monkeypatch, processo, linhas)
    assert erro.value.code == 1
    saida = capsys.readouterr()
    assert ausente in saida.err
    assert "metricas do" not in saida.out


def test_smoke_recusa_serie_obrigatoria_ausente(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    modulo = carregar_script("smoke_metricas", monkeypatch)
    ausente = modulo.SERIES["api"][0]
    linhas = [linha for linha in exposicao_completa(modulo, "api") if linha != ausente]
    with pytest.raises(SystemExit) as erro:
        executar_smoke(monkeypatch, "api", linhas)
    assert erro.value.code == 1
    assert ausente in capsys.readouterr().err


def test_o_catalogo_do_smoke_e_o_das_familias_que_o_codigo_define(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O smoke so olha o que esta no catalogo: uma familia nova no codigo e fora
    dele passaria sem ninguem ver, e uma que o codigo perdeu reprovaria o
    ``make smoke`` sem motivo. As tres gauges da outbox saem do coletor, que o
    processo registra no boot, e nao do modulo."""
    modulo = carregar_script("smoke_metricas", monkeypatch)
    for processo in ("main", "relay", "consumidor", "prazos"):
        importlib.import_module(f"src.{processo}")
    definidas = {
        familia.name
        for familia in REGISTRY.collect()
        if familia.name.startswith(("pytstop_", "http_request"))
    }
    catalogo = {nome for familias in modulo.FAMILIAS.values() for nome in familias}
    coletor = set(modulo.OUTBOX)
    assert definidas - coletor == catalogo - coletor
