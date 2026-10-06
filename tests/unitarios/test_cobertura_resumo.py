"""Resumo de cobertura do CI (scripts/cobertura_resumo.py), fora do src."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts" / "cobertura_resumo.py"
_spec = importlib.util.spec_from_file_location("cobertura_resumo", _SCRIPT)
assert _spec is not None
assert _spec.loader is not None
cobertura_resumo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cobertura_resumo)

COVERAGE_XML = """<?xml version="1.0" ?>
<coverage branch-rate="0.75" line-rate="0.8">
  <sources><source>/repo/src</source></sources>
  <packages><package name="x"><classes>
    <class filename="precos/dominio/preco.py">
      <lines><line number="1" hits="1"/><line number="2" hits="1"/></lines>
    </class>
    <class filename="precos/dominio/outro.py">
      <lines><line number="1" hits="0"/></lines>
    </class>
    <class filename="main.py">
      <lines><line number="1" hits="1"/><line number="2" hits="0"/></lines>
    </class>
  </classes></package></packages>
</coverage>
"""


def test_agrupa_por_contexto_e_camada(tmp_path: Path) -> None:
    arquivo = tmp_path / "coverage.xml"
    arquivo.write_text(COVERAGE_XML)
    assert cobertura_resumo.resumir(arquivo).splitlines() == [
        "### Cobertura de testes: 60.0% de linhas, 75.0% de ramos",
        "",
        "| Pacote | Linhas | Cobertas | Cobertura |",
        "|---|---:|---:|---:|",
        "| `src` | 2 | 1 | 50.0% |",
        "| `src/precos/dominio` | 3 | 2 | 66.7% |",
        "| **total** | 5 | 3 | 60.0% |",
    ]


def test_sem_coverage_xml_avisa_sem_falhar_o_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cobertura_resumo.main(["script", str(tmp_path / "nao.xml")]) == 0
    assert "nao foi gerado" in capsys.readouterr().out


def test_uso_errado(capsys: pytest.CaptureFixture[str]) -> None:
    assert cobertura_resumo.main(["script"]) == 2
    assert "uso:" in capsys.readouterr().err
