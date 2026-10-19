"""Resumo de cobertura do CI e do README (scripts/cobertura_resumo.py), fora do src."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conftest import carregar_script

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import ModuleType

COVERAGE_XML = """<?xml version="1.0" ?>
<coverage branch-rate="0.75" line-rate="0.6" lines-valid="5" lines-covered="3"
          branches-valid="4" branches-covered="3">
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
# Dois contextos e duas camadas, mais um pacote fora da lista de camadas.
XML_DOS_CONTEXTOS = """<?xml version="1.0" ?>
<coverage lines-valid="8" lines-covered="6" line-rate="0.75"
          branches-valid="4" branches-covered="2" branch-rate="0.5">
  <sources><source>/repo/src</source></sources>
  <packages><package name="x"><classes>
    <class filename="orcamento/dominio/orcamento.py"><lines>
      <line number="1" hits="1"/><line number="2" hits="3"/>
      <line number="3" hits="1"/><line number="4" hits="0"/>
    </lines></class>
    <class filename="pagamento/aplicacao/use_cases.py"><lines>
      <line number="1" hits="2"/>
    </lines></class>
    <class filename="pagamento/avulso/modulo.py"><lines>
      <line number="1" hits="0"/><line number="2" hits="5"/>
    </lines></class>
    <class filename="main.py"><lines>
      <line number="1" hits="1"/>
    </lines></class>
  </classes></package></packages>
</coverage>
"""
RUN = (
    "https://github.com/fiap-postech-sw-architecture/"
    "postech-sw-arch-p4-billing-service/actions/runs/39105281152"
)


@pytest.fixture(scope="module")
def script() -> Iterator[ModuleType]:
    with pytest.MonkeyPatch.context() as monkeypatch:
        yield carregar_script("cobertura_resumo", monkeypatch)


@pytest.fixture
def xml(tmp_path: Path) -> Path:
    arquivo = tmp_path / "coverage.xml"
    arquivo.write_text(XML_DOS_CONTEXTOS, encoding="utf-8")
    return arquivo


def test_agrupa_por_contexto_e_camada(script: ModuleType, tmp_path: Path) -> None:
    arquivo = tmp_path / "coverage.xml"
    arquivo.write_text(COVERAGE_XML, encoding="utf-8")
    assert script.resumir(arquivo).splitlines() == [
        "### Cobertura de testes: 66.7% no gate (linhas e ramos)",
        "",
        "60.0% de linhas e 75.0% de ramos.",
        "",
        "| Pacote | Linhas | Cobertas | Cobertura |",
        "|---|---:|---:|---:|",
        "| `src` | 2 | 1 | 50.0% |",
        "| `src/precos/dominio` | 3 | 2 | 66.7% |",
        "| **total** | 5 | 3 | 60.0% |",
    ]


def test_resumo_de_xml_sem_linha_nem_ramo_nao_divide_por_zero(
    script: ModuleType, tmp_path: Path
) -> None:
    arquivo = tmp_path / "coverage.xml"
    arquivo.write_text("<coverage><packages/></coverage>", encoding="utf-8")

    assert script.resumir(arquivo).splitlines() == [
        "### Cobertura de testes: 0.0% no gate (linhas e ramos)",
        "",
        "0.0% de linhas e 0.0% de ramos.",
        "",
        "| Pacote | Linhas | Cobertas | Cobertura |",
        "|---|---:|---:|---:|",
        "| **total** | 0 | 0 | 0.0% |",
    ]


def test_classe_sem_linha_nao_abre_linha_na_tabela(
    script: ModuleType, tmp_path: Path
) -> None:
    arquivo = tmp_path / "coverage.xml"
    arquivo.write_text(
        '<coverage><packages><package><classes><class filename="a/b/c.py"/>'
        "</classes></package></packages></coverage>",
        encoding="utf-8",
    )

    assert script.resumir(arquivo).splitlines()[-2:] == [
        "|---|---:|---:|---:|",
        "| **total** | 0 | 0 | 0.0% |",
    ]


def test_sem_coverage_xml_avisa_sem_falhar_o_job(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert script.main(["script", str(tmp_path / "nao.xml")]) == 0
    assert "nao foi gerado" in capsys.readouterr().out


def test_uso_errado(script: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert script.main(["script"]) == 2
    assert "uso:" in capsys.readouterr().err


def test_readme_sem_o_run_ou_com_argumento_a_mais_e_uso_errado(
    script: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert script.main(["script", "c.xml", "--readme", "README.md"]) == 2
    assert script.main(["script", "c.xml", "--run", RUN]) == 2
    assert capsys.readouterr().err.count("uso:") == 2


def test_main_escreve_o_resumo(
    script: ModuleType, xml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert script.main(["script", str(xml)]) == 0
    # Gate: (6 linhas + 2 ramos) / (8 + 4) = 66,7%.
    assert "66.7% no gate" in capsys.readouterr().out


def test_medir_separa_contexto_camada_e_os_processos_da_raiz(
    script: ModuleType, xml: Path
) -> None:
    medida = script.medir(xml)

    assert medida == script.Medida(
        contextos={
            "`src/orcamento`": (4, 3),
            "`src/pagamento`": (3, 2),
            "`src` (processos)": (1, 1),
        },
        camadas={"`dominio`": (4, 3), "`aplicacao`": (1, 1)},
        ramos=(4, 2),
    )


def test_secao_do_readme_em_portugues_com_o_link_do_run(
    script: ModuleType, xml: Path
) -> None:
    secao = script.secao_do_readme(script.medir(xml), RUN)

    # Gate: (6 linhas + 2 ramos) / (8 + 4) = 66,67%; os processos primeiro e as
    # camadas de dentro para fora (dominio primeiro).
    assert secao == (
        "**66,67% no gate** (linhas e ramos somados): 8 linhas, 6 cobertas "
        "(75,0%), e 4 ramos, 2 cobertos (50,0%), medidos pelo `make test` com o "
        f"`src/` do [run 39105281152 do CI]({RUN}). Tabelas geradas por "
        "`scripts/cobertura_resumo.py` a partir do `coverage.xml` desse "
        "`make test`.\n"
        "\n"
        "| Contexto | Linhas | Cobertas | Cobertura |\n"
        "|---|---:|---:|---:|\n"
        "| `src` (processos) | 1 | 1 | 100,0% |\n"
        "| `src/orcamento` | 4 | 3 | 75,0% |\n"
        "| `src/pagamento` | 3 | 2 | 66,7% |\n"
        "| **total** | 8 | 6 | 75,0% |\n"
        "\n"
        "| Camada | Linhas | Cobertas | Cobertura |\n"
        "|---|---:|---:|---:|\n"
        "| `dominio` | 4 | 3 | 75,0% |\n"
        "| `aplicacao` | 1 | 1 | 100,0% |\n"
    )


def test_milhar_com_ponto_na_secao_do_readme(script: ModuleType) -> None:
    medida = script.Medida(
        contextos={"`src/pagamento`": (2699, 2699)},
        camadas={"`dominio`": (1250, 1249)},
        ramos=(1000, 999),
    )

    secao = script.secao_do_readme(medida, RUN)

    assert "2.699 linhas, 2.699 cobertas (100,0%), e 1.000 ramos, 999" in secao
    assert "| `dominio` | 1.250 | 1.249 | 99,9% |" in secao


def test_medida_vazia_gera_zero_por_cento_sem_dividir_por_zero(
    script: ModuleType,
) -> None:
    vazia = script.Medida(contextos={}, camadas={}, ramos=(0, 0))

    secao = script.secao_do_readme(vazia, RUN)

    assert secao.startswith("**0,00% no gate** (linhas e ramos somados): 0 linhas, ")
    assert "| **total** | 0 | 0 | 0,0% |" in secao


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/fiap-postech-sw-architecture/x/actions/runs/",
        "https://github.com/o/r/actions/runs/123/job/456",
        "http://exemplo.com/actions/runs/123",
    ],
    ids=["run-sem-numero", "job-dentro-do-run", "fora-do-github"],
)
def test_secao_do_readme_so_aceita_o_endereco_de_um_run(
    script: ModuleType, url: str
) -> None:
    vazia = script.Medida(contextos={}, camadas={}, ramos=(0, 0))

    with pytest.raises(ValueError, match="endereco de um run do CI"):
        script.secao_do_readme(vazia, url)


def _argv(xml: Path, readme: Path, run: str = RUN) -> list[str]:
    return ["cobertura_resumo.py", str(xml), "--readme", str(readme), "--run", run]


def _readme(tmp_path: Path, meio: str) -> Path:
    readme = tmp_path / "README.md"
    readme.write_text(
        f"# Servico\n\n## Cobertura\n\n{meio}\nPara refazer.\n", encoding="utf-8"
    )
    return readme


def test_main_reescreve_so_o_trecho_entre_os_marcadores(
    script: ModuleType, xml: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readme = _readme(
        tmp_path, f"{script.MARCADOR_INICIO}\nvelha\n{script.MARCADOR_FIM}"
    )

    codigo = script.main(_argv(xml, readme))

    assert codigo == 0
    assert readme.read_text(encoding="utf-8") == (
        "# Servico\n\n## Cobertura\n\n"
        f"{script.MARCADOR_INICIO}\n"
        f"{script.secao_do_readme(script.medir(xml), RUN)}\n"
        f"{script.MARCADOR_FIM}\nPara refazer.\n"
    )
    assert capsys.readouterr().out == (
        f"{readme}: secao Cobertura reescrita a partir de {xml}\n"
    )


def test_reescrever_duas_vezes_deixa_o_mesmo_readme(
    script: ModuleType, xml: Path, tmp_path: Path
) -> None:
    readme = _readme(
        tmp_path, f"{script.MARCADOR_INICIO}\nvelha\n{script.MARCADOR_FIM}"
    )

    assert script.main(_argv(xml, readme)) == 0
    primeira = readme.read_text(encoding="utf-8")
    assert script.main(_argv(xml, readme)) == 0

    assert readme.read_text(encoding="utf-8") == primeira


@pytest.mark.parametrize(
    "meio",
    ["sem marcador", "{FIM}\n{INICIO}", "{INICIO}\n{FIM}\n{INICIO}\n{FIM}"],
    ids=["ausente", "invertido", "repetido"],
)
def test_marcador_ausente_repetido_ou_invertido_reprova_sem_tocar_no_readme(
    script: ModuleType,
    xml: Path,
    tmp_path: Path,
    meio: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    meio = meio.format(INICIO=script.MARCADOR_INICIO, FIM=script.MARCADOR_FIM)
    readme = _readme(tmp_path, meio)
    antes = readme.read_text(encoding="utf-8")

    codigo = script.main(_argv(xml, readme))

    assert codigo == 1
    assert readme.read_text(encoding="utf-8") == antes
    assert "cobertura:" in capsys.readouterr().err
    with pytest.raises(ValueError, match="cobertura:"):
        script.atualizar_readme(antes, "x\n")


def test_run_que_nao_e_de_um_run_do_ci_reprova_sem_tocar_no_readme(
    script: ModuleType, xml: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readme = _readme(
        tmp_path, f"{script.MARCADOR_INICIO}\nvelha\n{script.MARCADOR_FIM}"
    )
    antes = readme.read_text(encoding="utf-8")

    assert script.main(_argv(xml, readme, "https://exemplo.com/run/1")) == 1
    assert readme.read_text(encoding="utf-8") == antes
    assert "endereco de um run do CI" in capsys.readouterr().err


def test_coverage_ausente_reprova_a_reescrita_com_uma_linha_e_sem_traceback(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readme = _readme(
        tmp_path, f"{script.MARCADOR_INICIO}\nvelha\n{script.MARCADOR_FIM}"
    )
    antes = readme.read_text(encoding="utf-8")
    ausente = tmp_path / "coverage.xml"

    codigo = script.main(_argv(ausente, readme))

    assert codigo == 1
    assert capsys.readouterr().err == (
        f"{ausente} nao existe: rode make test ou baixe o coverage.xml do run\n"
    )
    assert readme.read_text(encoding="utf-8") == antes


@pytest.mark.parametrize("quebra", ["ausente", "diretorio", "binario"])
def test_readme_ausente_ilegivel_ou_que_nao_e_texto_reprova_com_uma_linha(
    script: ModuleType,
    xml: Path,
    tmp_path: Path,
    quebra: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    readme = tmp_path / "README.md"
    if quebra == "diretorio":
        readme.mkdir()
    elif quebra == "binario":
        readme.write_bytes(b"\xff\xfe# Servico\n")

    assert script.main(_argv(xml, readme)) == 1

    erro = capsys.readouterr().err
    assert erro.count("\n") == 1
    assert "Traceback" not in erro


def test_coverage_que_e_um_diretorio_reprova_a_reescrita_sem_traceback(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readme = _readme(
        tmp_path, f"{script.MARCADOR_INICIO}\nvelha\n{script.MARCADOR_FIM}"
    )
    antes = readme.read_text(encoding="utf-8")
    diretorio = tmp_path / "coverage.xml"
    diretorio.mkdir()

    assert script.main(_argv(diretorio, readme)) == 1
    assert capsys.readouterr().err.count("\n") == 1
    assert readme.read_text(encoding="utf-8") == antes


@pytest.mark.parametrize(
    "conteudo", ["<html/>", "<coverage/>"], ids=["outro-xml", "coverage-vazio"]
)
def test_coverage_sem_nenhuma_linha_reprova_a_reescrita_em_vez_de_zerar_a_tabela(
    script: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    conteudo: str,
) -> None:
    readme = _readme(
        tmp_path, f"{script.MARCADOR_INICIO}\nvelha\n{script.MARCADOR_FIM}"
    )
    antes = readme.read_text(encoding="utf-8")
    vazio = tmp_path / "coverage.xml"
    vazio.write_text(conteudo, encoding="utf-8")

    assert script.main(_argv(vazio, readme)) == 1
    erro = capsys.readouterr().err
    assert erro.count("\n") == 1
    assert "nao traz nenhuma linha de cobertura" in erro
    assert readme.read_text(encoding="utf-8") == antes


def test_coverage_que_nao_e_xml_reprova_a_reescrita_sem_traceback(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readme = _readme(
        tmp_path, f"{script.MARCADOR_INICIO}\nvelha\n{script.MARCADOR_FIM}"
    )
    antes = readme.read_text(encoding="utf-8")
    quebrado = tmp_path / "coverage.xml"
    quebrado.write_text("<coverage>", encoding="utf-8")

    assert script.main(_argv(quebrado, readme)) == 1
    assert capsys.readouterr().err.count("\n") == 1
    assert readme.read_text(encoding="utf-8") == antes
