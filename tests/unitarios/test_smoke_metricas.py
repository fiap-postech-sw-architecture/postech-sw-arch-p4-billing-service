"""O smoke exige familias e series: positivo e negativos da entrada real."""

from __future__ import annotations

import io
import runpy
import sys
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

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


def linhas_completas(modulo: ModuleType, processo: str) -> list[str]:
    linhas = [f"{nome} 0.0" for nome in modulo.FAMILIAS[processo]]
    linhas += [s + "0.0" if s.endswith(" ") else s for s in modulo.SERIES[processo]]
    return linhas


def executar_smoke(
    monkeypatch: pytest.MonkeyPatch, processo: str, linhas: list[str]
) -> None:
    def abrir(url: str, *, timeout: int) -> io.BytesIO:
        assert (url, timeout) == (URL, 5)
        return io.BytesIO("\n".join(linhas).encode())

    monkeypatch.setattr(urllib.request, "urlopen", abrir)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), processo, URL])
    runpy.run_path(str(SCRIPT), run_name="__main__")


@pytest.mark.parametrize("processo", list(FAMILIA_A_REMOVER))
def test_smoke_aceita_todas_as_familias_e_series(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], processo: str
) -> None:
    modulo = carregar_script("smoke_metricas", monkeypatch)
    executar_smoke(monkeypatch, processo, linhas_completas(modulo, processo))
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
    linhas = [
        linha
        for linha in linhas_completas(modulo, processo)
        if modulo.familia(linha) != ausente
    ]
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
    linhas = [linha for linha in linhas_completas(modulo, "api") if linha != ausente]
    with pytest.raises(SystemExit) as erro:
        executar_smoke(monkeypatch, "api", linhas)
    assert erro.value.code == 1
    assert ausente in capsys.readouterr().err
