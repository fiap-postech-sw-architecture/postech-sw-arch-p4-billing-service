"""Resumo da cobertura em Markdown a partir do coverage.xml (formato Cobertura).

Uso: python scripts/cobertura_resumo.py coverage.xml >> "$GITHUB_STEP_SUMMARY"

Agrupa por contexto e camada (`src/<contexto>/<camada>`) para a evidencia de
cobertura pedida no enunciado ficar legivel no summary do CI e no README.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree  # nosec B405 - arquivo gerado pelo proprio pytest-cov

PROFUNDIDADE = 2  # <contexto>/<camada>, relativo ao <source> (relative_files = True)
_ARGUMENTOS = 2  # programa + caminho do coverage.xml


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
    saida = [
        f"### Cobertura de testes: {pct_total:.1f}% de linhas, {ramo:.1f}% de ramos",
        "",
        "| Pacote | Linhas | Cobertas | Cobertura |",
        "|---|---:|---:|---:|",
    ]
    for grupo in sorted(linhas):
        tot, cob = linhas[grupo]
        pct = 100.0 * cob / tot if tot else 0.0
        saida.append(f"| `{grupo}` | {tot} | {cob} | {pct:.1f}% |")
    saida.append(
        f"| **total** | {total_linhas} | {total_cobertas} | {pct_total:.1f}% |"
    )
    return "\n".join(saida) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != _ARGUMENTOS:
        print("uso: python scripts/cobertura_resumo.py coverage.xml", file=sys.stderr)
        return 2
    caminho = Path(argv[1])
    if not caminho.exists():
        print(
            f"### Cobertura de testes\n\n`{caminho}` nao foi gerado "
            "(a suite falhou antes).\n"
        )
        return 0
    sys.stdout.write(resumir(caminho))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
