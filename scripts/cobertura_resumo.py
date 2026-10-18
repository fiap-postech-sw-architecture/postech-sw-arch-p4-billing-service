"""Resumo da cobertura em Markdown a partir do coverage.xml (formato Cobertura).

Uso:
    python scripts/cobertura_resumo.py coverage.xml >> "$GITHUB_STEP_SUMMARY"
    python scripts/cobertura_resumo.py coverage.xml --readme README.md --run URL

Sem opcoes, agrupa por contexto e camada (`src/<contexto>/<camada>`) para a
evidencia de cobertura pedida no enunciado ficar legivel no summary do CI. Com
``--readme``, reescreve a secao Cobertura do README entre os marcadores
``<!-- cobertura:inicio -->`` e ``<!-- cobertura:fim -->``: uma tabela por
contexto e outra por camada, com o link do run do CI que tem o mesmo ``src/``
(``make cobertura-readme RUN=<id>`` baixa o ``coverage.xml`` desse run).
``tests/unitarios/test_documentacao.py`` reprova a tabela que nao saiu daqui.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

# A entrada e o coverage.xml gerado pelo proprio pytest-cov (confiavel).
from xml.etree import ElementTree  # nosec B405

PROFUNDIDADE = 2  # <contexto>/<camada>, relativo ao <source> (relative_files = True)
_USO = (
    "uso: python scripts/cobertura_resumo.py coverage.xml "
    "[--readme README.md --run URL_DO_RUN_DO_CI]"
)

MARCADOR_INICIO = "<!-- cobertura:inicio -->"
MARCADOR_FIM = "<!-- cobertura:fim -->"
PROCESSOS = "`src` (processos)"
CAMADAS = ("dominio", "aplicacao", "infraestrutura", "interfaces")
_RUN_DO_CI = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/actions/runs/(\d+)")


def _grupo(nome_arquivo: str, prefixo: str) -> str:
    partes = Path(nome_arquivo).parts[:-1][:PROFUNDIDADE]
    return "/".join([prefixo, *partes]) if partes else prefixo


def resumir(caminho: Path) -> str:
    # Entrada confiavel: XML gerado pelo pytest-cov no mesmo job.
    raiz = ElementTree.parse(caminho).getroot()  # noqa: S314  # nosec B314
    fontes = [s.text or "." for s in raiz.iter("source")]
    prefixo = Path(fontes[0]).name if len(fontes) == 1 else "."
    linhas: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for classe in raiz.iter("class"):
        grupo = _grupo(classe.get("filename", ""), prefixo)
        for linha in classe.iter("line"):
            linhas[grupo][0] += 1
            if int(linha.get("hits", "0")) > 0:
                linhas[grupo][1] += 1
    total_linhas = sum(v[0] for v in linhas.values())
    total_cobertas = sum(v[1] for v in linhas.values())
    pct_total = 100.0 * total_cobertas / total_linhas if total_linhas else 0.0
    ramo = float(raiz.get("branch-rate", "0")) * 100
    # O numero do gate (fail_under, branch = True) soma linhas e ramos.
    validos = int(raiz.get("lines-valid", "0")) + int(raiz.get("branches-valid", "0"))
    cobertos = int(raiz.get("lines-covered", "0")) + int(
        raiz.get("branches-covered", "0")
    )
    gate = 100.0 * cobertos / validos if validos else 0.0
    saida = [
        f"### Cobertura de testes: {gate:.1f}% no gate (linhas e ramos)",
        "",
        f"{pct_total:.1f}% de linhas e {ramo:.1f}% de ramos.",
        "",
        "| Pacote | Linhas | Cobertas | Cobertura |",
        "|---|---:|---:|---:|",
    ]
    for grupo in sorted(linhas):
        tot, cob = linhas[grupo]
        pct = 100.0 * cob / tot  # o grupo so existe com ao menos uma linha
        saida.append(f"| `{grupo}` | {tot} | {cob} | {pct:.1f}% |")
    saida.append(
        f"| **total** | {total_linhas} | {total_cobertas} | {pct_total:.1f}% |"
    )
    return "\n".join(saida) + "\n"


@dataclass(frozen=True)
class Medida:
    """Linhas (total, cobertas) por rotulo de contexto e de camada, e os ramos."""

    contextos: dict[str, tuple[int, int]]
    camadas: dict[str, tuple[int, int]]
    ramos: tuple[int, int]


def medir(caminho: Path) -> Medida:
    """Conta as linhas por contexto (`src/<contexto>`) e por camada.

    Os modulos da raiz de `src` (os processos) formam um contexto proprio e nao
    entram em camada nenhuma.
    """
    raiz = ElementTree.parse(caminho).getroot()  # noqa: S314  # nosec B314
    contextos: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    camadas: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for classe in raiz.iter("class"):
        partes = Path(classe.get("filename", "")).parts
        contexto = f"`src/{partes[0]}`" if len(partes) > 1 else PROCESSOS
        camada = partes[1] if len(partes) > PROFUNDIDADE else ""
        for linha in classe.iter("line"):
            coberta = int(linha.get("hits", "0")) > 0
            contextos[contexto][0] += 1
            contextos[contexto][1] += coberta
            if camada in CAMADAS:
                camadas[f"`{camada}`"][0] += 1
                camadas[f"`{camada}`"][1] += coberta
    return Medida(
        contextos={k: (v[0], v[1]) for k, v in contextos.items()},
        camadas={k: (v[0], v[1]) for k, v in camadas.items()},
        ramos=(
            int(raiz.get("branches-valid", "0")),
            int(raiz.get("branches-covered", "0")),
        ),
    )


def _inteiro(valor: int) -> str:
    return f"{valor:,}".replace(",", ".")


def _pct(parte: int, todo: int, casas: int = 1) -> str:
    return f"{100.0 * parte / todo if todo else 0.0:.{casas}f}".replace(".", ",") + "%"


def _tabela(titulo: str, linhas: dict[str, tuple[int, int]]) -> list[str]:
    return [
        f"| {titulo} | Linhas | Cobertas | Cobertura |",
        "|---|---:|---:|---:|",
        *(
            f"| {rotulo} | {_inteiro(tot)} | {_inteiro(cob)} | {_pct(cob, tot)} |"
            for rotulo, (tot, cob) in linhas.items()
        ),
    ]


def secao_do_readme(medida: Medida, url_do_run: str) -> str:
    """O texto entre os marcadores da secao Cobertura do README.

    Raises:
        ValueError: ``url_do_run`` nao e o endereco de um run do GitHub Actions.
    """
    encontrado = _RUN_DO_CI.fullmatch(url_do_run)
    if encontrado is None:
        msg = f"--run precisa ser o endereco de um run do CI: {url_do_run!r}"
        raise ValueError(msg)
    contextos = dict(
        sorted(medida.contextos.items(), key=lambda par: (par[0] != PROCESSOS, par[0]))
    )
    rotulos = [f"`{camada}`" for camada in CAMADAS]
    camadas = {r: medida.camadas[r] for r in rotulos if r in medida.camadas}
    linhas = sum(tot for tot, _ in contextos.values())
    cobertas = sum(cob for _, cob in contextos.values())
    ramos, ramos_cobertos = medida.ramos
    gate = _pct(cobertas + ramos_cobertos, linhas + ramos, casas=2)
    texto = [
        f"**{gate} no gate** (linhas e ramos somados): {_inteiro(linhas)} linhas, "
        f"{_inteiro(cobertas)} cobertas ({_pct(cobertas, linhas)}), e "
        f"{_inteiro(ramos)} ramos, {_inteiro(ramos_cobertos)} cobertos "
        f"({_pct(ramos_cobertos, ramos)}), medidos pelo `make test` com o `src/` do "
        f"[run {encontrado.group(1)} do CI]({url_do_run}). Tabelas geradas por "
        "`scripts/cobertura_resumo.py` a partir do `coverage.xml` desse `make test`.",
        "",
        *_tabela("Contexto", contextos),
        f"| **total** | {_inteiro(linhas)} | {_inteiro(cobertas)} | "
        f"{_pct(cobertas, linhas)} |",
        "",
        *_tabela("Camada", camadas),
    ]
    return "\n".join(texto) + "\n"


def atualizar_readme(texto: str, secao: str) -> str:
    """Troca o que esta entre os marcadores da Cobertura por ``secao``.

    A linha em branco antes do marcador de fim encerra a tabela no Markdown.

    Raises:
        ValueError: os marcadores faltam, se repetem ou estao fora de ordem.
    """
    if texto.count(MARCADOR_INICIO) != 1 or texto.count(MARCADOR_FIM) != 1:
        msg = f"o README precisa de um {MARCADOR_INICIO} e um {MARCADOR_FIM}"
        raise ValueError(msg)
    antes, resto = texto.split(MARCADOR_INICIO)
    if MARCADOR_FIM not in resto:
        msg = f"{MARCADOR_FIM} vem antes de {MARCADOR_INICIO} no README"
        raise ValueError(msg)
    _, depois = resto.split(MARCADOR_FIM)
    return f"{antes}{MARCADOR_INICIO}\n{secao}\n{MARCADOR_FIM}{depois}"


def _reescrever_readme(xml: Path, readme: Path, url_do_run: str) -> int:
    if not xml.exists():
        print(
            f"{xml} nao existe: rode make test ou baixe o coverage.xml do run",
            file=sys.stderr,
        )
        return 1
    try:
        secao = secao_do_readme(medir(xml), url_do_run)
        texto = atualizar_readme(readme.read_text(encoding="utf-8"), secao)
        readme.write_text(texto, encoding="utf-8")
    except (OSError, ValueError, ElementTree.ParseError) as erro:
        print(erro, file=sys.stderr)
        return 1
    print(f"{readme}: secao Cobertura reescrita a partir de {xml}")
    return 0


def main(argv: list[str]) -> int:
    match argv[1:]:
        case [xml]:
            caminho = Path(xml)
        case [xml, "--readme", readme, "--run", url_do_run]:
            return _reescrever_readme(Path(xml), Path(readme), url_do_run)
        case _:
            print(_USO, file=sys.stderr)
            return 2
    if not caminho.exists():
        print(
            f"### Cobertura de testes\n\n`{caminho}` nao foi gerado "
            "(a suite falhou antes).\n"
        )
        return 0
    sys.stdout.write(resumir(caminho))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
