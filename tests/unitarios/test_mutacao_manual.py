"""Lista de mutantes manuais (scripts/mutantes.toml) e relatorio do
scripts/mutacao_manual.py, fora do src."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

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


_CALCULADORA = "def dobro(x):\n    return x * 2\n"
_TESTE = (
    "from calculadora import dobro\n\n\ndef test_dobro():\n    assert dobro(2) == 4\n"
)
_GRUPO = ("tests/test_calculadora.py",)


def _mutante(
    para: str, equivalente: str = "", testes: tuple[str, ...] = _GRUPO
) -> object:
    return mutacao_manual.Mutante(
        "T1", "calc", "calculadora.py", testes, "return x * 2", para, equivalente
    )


@pytest.fixture
def copia(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Um repositorio minusculo (um modulo e um teste) e a copia dele."""
    raiz = tmp_path / "raiz"
    (raiz / "tests").mkdir(parents=True)
    (raiz / "calculadora.py").write_text(_CALCULADORA, encoding="utf-8")
    (raiz / "tests" / "test_calculadora.py").write_text(_TESTE, encoding="utf-8")
    (raiz / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n", encoding="utf-8"
    )
    monkeypatch.setattr(mutacao_manual, "RAIZ", raiz)
    monkeypatch.setattr(mutacao_manual, "COPIA", tmp_path / "copia")
    mutacao_manual.preparar_copia(("calculadora.py", "tests", "pyproject.toml"))
    return tmp_path / "copia"


@pytest.mark.parametrize(
    ("mutante", "esperado"),
    [
        pytest.param(
            _mutante("return x * 3"),
            ("morto", "test_calculadora.py::test_dobro"),
            id="morto",
        ),
        pytest.param(
            _mutante("return x * 3", "dizia ser equivalente"),
            ("morto", "test_calculadora.py::test_dobro"),
            id="equivalente-que-morre-conta-como-morto",
        ),
        pytest.param(_mutante("return x + x"), ("sobreviveu", "-"), id="sobrevivente"),
        pytest.param(
            _mutante("return x + x", "x + x e x * 2 dao o mesmo"),
            ("equivalente", "-"),
            id="equivalente-que-sobrevive",
        ),
        pytest.param(
            _mutante("return x *"),
            ("erro", "pytest saiu com 2, sem teste que falhou"),
            id="pytest-quebrado",
        ),
    ],
)
def test_rodar_um_traduz_a_saida_do_pytest_em_resultado(
    copia: Path, mutante: object, esperado: tuple[str, str]
) -> None:
    assert mutacao_manual._rodar_um(mutante) == esperado
    assert (copia / "calculadora.py").read_text(encoding="utf-8") == _CALCULADORA


def test_rodar_um_conta_o_tempo_esgotado_como_morto(
    copia: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (copia / "tests" / "test_lento.py").write_text(
        "import time\n\n\ndef test_lento():\n    time.sleep(30)\n", encoding="utf-8"
    )
    monkeypatch.setattr(mutacao_manual, "LIMITE_DE_UM_MUTANTE_SEGUNDOS", 1)

    resultado = mutacao_manual._rodar_um(
        _mutante("return x * 3", testes=("tests/test_lento.py",))
    )

    assert resultado == ("morto", "(tempo esgotado)")
    assert (copia / "calculadora.py").read_text(encoding="utf-8") == _CALCULADORA


def test_grupo_que_ja_falha_na_copia_limpa_nao_mata_ninguem(
    copia: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (copia / "tests" / "test_ja_falha.py").write_text(
        "def test_ja_falha():\n    assert False\n", encoding="utf-8"
    )
    ja_falha = ("tests/test_ja_falha.py",)
    chamadas: list[tuple[str, ...]] = []
    de_verdade = mutacao_manual._pytest

    def espiar(testes: tuple[str, ...]) -> object:
        chamadas.append(testes)
        return de_verdade(testes)

    monkeypatch.setattr(mutacao_manual, "_pytest", espiar)

    linhas = mutacao_manual.executar(
        (
            _mutante("return x * 3"),
            _mutante("return x + x"),
            _mutante("return x * 3", testes=ja_falha),
            _mutante("return x + x", "equivalente", testes=ja_falha),
        ),
        600,
    )

    motivo = "teste do grupo reprova na copia limpa: test_ja_falha.py::test_ja_falha"
    assert [(x["resultado"], x["teste"]) for x in linhas] == [
        ("morto", "test_calculadora.py::test_dobro"),
        ("sobreviveu", "-"),
        ("erro", motivo),
        ("erro", motivo),
    ]
    # Cada grupo roda limpo uma vez; o que reprova limpo nao roda mais nenhum.
    assert chamadas.count(_GRUPO) == 3
    assert chamadas.count(ja_falha) == 1


def test_grupo_que_estoura_o_tempo_na_copia_limpa_sai_como_erro(
    copia: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (copia / "tests" / "test_lento.py").write_text(
        "import time\n\n\ndef test_lento():\n    time.sleep(30)\n", encoding="utf-8"
    )
    monkeypatch.setattr(mutacao_manual, "LIMITE_DE_UM_MUTANTE_SEGUNDOS", 1)

    (linha,) = mutacao_manual.executar(
        (_mutante("return x * 3", testes=("tests/test_lento.py",)),), 600
    )

    assert (linha["resultado"], linha["teste"]) == (
        "erro",
        "teste do grupo reprova na copia limpa: (tempo esgotado)",
    )


def _linhas(*resultados: str) -> list[dict[str, str]]:
    return [
        {"id": str(i), "area": "x", "arquivo": "a.py", "mudanca": "m", "teste": ""}
        | {"resultado": resultado}
        for i, resultado in enumerate(resultados)
    ]


def test_relatorio_sem_percentual_quando_ha_erro_ou_mutante_nao_rodado() -> None:
    inteiro = mutacao_manual.relatorio(_linhas("morto", "morto"), 60.0)
    com_erro = mutacao_manual.relatorio(_linhas("morto", "morto", "erro"), 60.0)
    cortado = mutacao_manual.relatorio(_linhas("morto", "morto", "nao rodado"), 60.0)

    assert inteiro.startswith("Mutantes manuais: 2 mortos de 2 (100.0%);")
    assert com_erro.startswith("Mutantes manuais: 2 mortos de 2 (n/d);")
    assert cortado.startswith("Mutantes manuais: 2 mortos de 2 (n/d);")


def test_conferir_aponta_arquivo_inexistente_sem_levantar() -> None:
    sumido = mutacao_manual.Mutante("X1", "a", "src/nao_existe.py", (), "a", "b")

    assert mutacao_manual.conferir((sumido,)) == [
        "X1: arquivo inexistente: src/nao_existe.py"
    ]


def test_main_recusa_arquivo_inexistente_com_mensagem(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sumido = mutacao_manual.Mutante("X1", "a", "src/nao_existe.py", (), "a", "b")
    plano = mutacao_manual.Plano((), (sumido,))
    monkeypatch.setattr(mutacao_manual, "carregar", lambda: plano)

    assert mutacao_manual.main([]) == 2
    assert "X1: arquivo inexistente: src/nao_existe.py" in capsys.readouterr().err


def test_main_recusa_id_desconhecido_e_lista_os_validos(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert mutacao_manual.main(["--so", "D01", "d06", "X99"]) == 2

    erro = capsys.readouterr().err
    assert "ids desconhecidos: d06 X99" in erro
    assert "ids validos: D01 D02 D03" in erro


@pytest.mark.parametrize(
    ("resultados", "saida"),
    [
        pytest.param(("morto", "equivalente"), 0, id="so-mortos-e-equivalentes"),
        pytest.param(("morto", "sobreviveu"), 0, id="sobrevivente-nao-reprova"),
        pytest.param(("morto", "nao rodado"), 0, id="prazo-curto-nao-reprova"),
        pytest.param(("morto", "erro"), 1, id="erro-reprova"),
    ],
)
def test_main_so_sai_com_erro_quando_a_medida_e_invalida(
    resultados: tuple[str, ...],
    saida: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    linhas = _linhas(*resultados)
    monkeypatch.setattr(mutacao_manual, "RELATORIO", tmp_path / "reports" / "r.md")
    monkeypatch.setattr(mutacao_manual, "preparar_copia", lambda copiar: None)
    monkeypatch.setattr(mutacao_manual, "executar", lambda mutantes, prazo: linhas)

    assert mutacao_manual.main(["--so", "D01"]) == saida
    texto = (tmp_path / "reports" / "r.md").read_text(encoding="utf-8")
    assert texto.startswith("Mutantes manuais:")
