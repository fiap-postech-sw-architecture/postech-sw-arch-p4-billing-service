"""Lista de mutantes manuais (scripts/mutantes.toml) e relatorio do
scripts/mutacao_manual.py, fora do src."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "mutacao_manual.py"
_spec = importlib.util.spec_from_file_location("mutacao_manual", _SCRIPT)
assert _spec is not None
assert _spec.loader is not None
mutacao_manual = importlib.util.module_from_spec(_spec)
sys.modules["mutacao_manual"] = mutacao_manual
_spec.loader.exec_module(mutacao_manual)


def test_todo_mutante_se_aplica_uma_so_vez_ao_codigo_atual() -> None:
    mutantes = mutacao_manual.carregar().mutantes

    assert len(mutantes) > 0
    assert mutacao_manual.conferir(mutantes) == []


def test_config_copia_o_codigo_e_os_testes_e_tudo_existe() -> None:
    copiar = mutacao_manual.carregar().copiar

    assert {"src", "tests"} <= set(copiar)
    assert [n for n in copiar if not (mutacao_manual.RAIZ / n).exists()] == []


def test_conferir_aponta_trecho_ausente_repetido_e_mutante_nulo() -> None:
    base = mutacao_manual.carregar().mutantes[0]
    ausente = mutacao_manual.Mutante(
        "X1", "a", base.arquivo, base.testes, "nao existe no arquivo", "y"
    )
    repetido = mutacao_manual.Mutante("X2", "a", base.arquivo, base.testes, "def ", "y")
    nulo = mutacao_manual.Mutante(
        "X3", "a", base.arquivo, base.testes, base.de, base.de
    )

    problemas = mutacao_manual.conferir((ausente, repetido, nulo, nulo))

    assert [p.split(":")[0] for p in problemas] == ["X3", "X1", "X2", "X3", "X3"]


def test_relatorio_deixa_o_equivalente_fora_da_conta() -> None:
    linhas = [
        {"id": "A", "area": "x", "arquivo": "a.py", "mudanca": "m", "teste": "t"},
        {"id": "B", "area": "x", "arquivo": "b.py", "mudanca": "m", "teste": "-"},
        {"id": "C", "area": "x", "arquivo": "c.py", "mudanca": "m", "teste": "-"},
    ]
    for linha, resultado in zip(
        linhas, ["morto", "sobreviveu", "equivalente"], strict=True
    ):
        linha["resultado"] = resultado

    texto = mutacao_manual.relatorio(linhas, 60.0)

    assert texto.startswith(
        "Mutantes manuais: 1 mortos de 2 (50.0%); 1 sobreviveram, "
        "1 equivalentes ficaram de fora da conta, 0 com erro e 0 nao rodados."
    )
