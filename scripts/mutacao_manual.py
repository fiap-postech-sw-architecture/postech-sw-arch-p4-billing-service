"""Mutantes manuais dos modulos criticos (``make mutacao``).

O mutmut nao roda aqui. Ele exige que o pacote do codigo seja importado sem o
prefixo ``src.`` (layout ``src/<pacote>``), e o pacote do servico se chama
``src``: a primeira chamada de uma funcao mutada reprova com
``AssertionError: Failed trampoline hit. Module name starts with `src.`, which
is invalid`` (mutmut 3.8.0, ``mutmut/stats.py``, ``record_trampoline_hit``).
Renomear o pacote ou remendar a ferramenta mudaria o que se mede, entao cada
mutante de ``mutantes.toml`` e um defeito plausivel, escrito a mao, num modulo
critico.

O script copia para ``mutants/`` (fora do git) o que ``[config]`` lista em
``copiar``, aplica um mutante por vez nessa copia, roda so os testes do grupo
dele e diz qual falhou primeiro. Antes do primeiro mutante de cada grupo, roda
o grupo na copia sem mutante: teste que ja falha mataria qualquer um, entao os
mutantes desse grupo saem ``erro``. Nao e gate de CI nem tem pontuacao minima:
o resultado vai para o README (Mutacao). ``--prazo`` (segundos) para de iniciar
mutantes quando estoura; os que sobram saem como ``nao rodado``.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess  # nosec B404 - roda o pytest do proprio repositorio
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
MUTANTES_TOML = RAIZ / "scripts" / "mutantes.toml"
COPIA = RAIZ / "mutants"
RELATORIO = RAIZ / "reports" / "mutacao.md"
PRAZO_PADRAO_SEGUNDOS = 900
LIMITE_DE_UM_MUTANTE_SEGUNDOS = 240


@dataclass(frozen=True, slots=True)
class Mutante:
    """Um defeito plausivel: ``de`` (uma ocorrencia exata) vira ``para``."""

    id: str
    area: str
    arquivo: str
    testes: tuple[str, ...]
    de: str
    para: str
    equivalente: str = ""


@dataclass(frozen=True, slots=True)
class Plano:
    """O que o ``mutantes.toml`` diz: o que copiar e os defeitos a aplicar."""

    copiar: tuple[str, ...]
    mutantes: tuple[Mutante, ...]


def carregar() -> Plano:
    dados = tomllib.loads(MUTANTES_TOML.read_text(encoding="utf-8"))
    mutantes = tuple(
        Mutante(
            id=m["id"],
            area=m["area"],
            arquivo=m["arquivo"],
            testes=tuple(dados["testes"][m["testes"]]),
            de=m["de"],
            para=m["para"],
            equivalente=m.get("equivalente", ""),
        )
        for m in dados["mutante"]
    )
    return Plano(copiar=tuple(dados["config"]["copiar"]), mutantes=mutantes)


def conferir(mutantes: tuple[Mutante, ...]) -> list[str]:
    """Problemas: id repetido, arquivo ou trecho ausente, trecho repetido, nulo."""
    ids = [m.id for m in mutantes]
    problemas = [f"{i}: id repetido" for i in sorted(set(ids)) if ids.count(i) > 1]
    for m in mutantes:
        alvo = RAIZ / m.arquivo
        if not alvo.is_file():
            problemas.append(f"{m.id}: arquivo inexistente: {m.arquivo}")
        elif (ocorrencias := alvo.read_text(encoding="utf-8").count(m.de)) != 1:
            problemas.append(f"{m.id}: o trecho aparece {ocorrencias}x em {m.arquivo}")
        if m.de == m.para:
            problemas.append(f"{m.id}: o mutante nao muda nada")
    return problemas


def _resumir(texto: str) -> str:
    return " ".join(texto.split()) or "(removido)"


def _pytest(testes: tuple[str, ...]) -> tuple[int, str, bool]:
    comando = [
        *(sys.executable, "-m", "pytest", *testes, "-x", "-q", "--no-header"),
        *("-p", "no:cacheprovider", "-o", "addopts=--strict-markers"),
        *("-o", "timeout=60", "-rf"),
    ]
    # Sem .pyc: o mutante troca o arquivo no mesmo segundo e com o mesmo tamanho.
    ambiente = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        resultado = subprocess.run(  # noqa: S603  # nosec B603 - pytest do repositorio
            comando,
            cwd=COPIA,
            env=ambiente,
            capture_output=True,
            text=True,
            check=False,
            timeout=LIMITE_DE_UM_MUTANTE_SEGUNDOS,
        )
    except subprocess.TimeoutExpired:
        return 1, "", True
    return resultado.returncode, resultado.stdout + resultado.stderr, False


def _primeiro_que_falhou(saida: str) -> str:
    achado = re.search(r"^FAILED (\S+)", saida, re.MULTILINE)
    return achado.group(1).removeprefix("tests/") if achado else ""


def preparar_copia(copiar: tuple[str, ...]) -> None:
    shutil.rmtree(COPIA, ignore_errors=True)
    COPIA.mkdir()
    for nome in copiar:
        origem = RAIZ / nome
        if origem.is_dir():
            shutil.copytree(
                origem, COPIA / nome, ignore=shutil.ignore_patterns("__pycache__")
            )
        else:
            shutil.copyfile(origem, COPIA / nome)


def _rodar_um(m: Mutante) -> tuple[str, str]:
    alvo = COPIA / m.arquivo
    original = (RAIZ / m.arquivo).read_text(encoding="utf-8")
    alvo.write_text(original.replace(m.de, m.para, 1), encoding="utf-8")
    try:
        codigo, saida, estourou = _pytest(m.testes)
    finally:
        alvo.write_text(original, encoding="utf-8")
    if codigo == 0:
        return ("equivalente" if m.equivalente else "sobreviveu"), "-"
    if estourou:
        return "morto", "(tempo esgotado)"
    # Morto so com teste que falhou: erro de coleta ou de fixture (Docker fora,
    # mutante que nao importa) nao prova nada sobre a suite.
    if codigo == 1 and (falhou := _primeiro_que_falhou(saida)):
        return "morto", falhou
    return "erro", f"pytest saiu com {codigo}, sem teste que falhou"


def _motivo_de_o_grupo_reprovar(testes: tuple[str, ...]) -> str:
    """Vazio se o grupo passa na copia sem mutante; senao, o que o reprova."""
    codigo, saida, estourou = _pytest(testes)
    if codigo == 0:
        return ""
    if estourou:
        return "(tempo esgotado)"
    return _primeiro_que_falhou(saida) or f"pytest saiu com {codigo}"


def _resultado(m: Mutante, limpos: dict[tuple[str, ...], str]) -> tuple[str, str]:
    if m.testes not in limpos:
        limpos[m.testes] = _motivo_de_o_grupo_reprovar(m.testes)
    if motivo := limpos[m.testes]:
        return "erro", f"teste do grupo reprova na copia limpa: {motivo}"
    return _rodar_um(m)


def executar(mutantes: tuple[Mutante, ...], prazo: float) -> list[dict[str, str]]:
    inicio = time.monotonic()
    linhas = []
    limpos: dict[tuple[str, ...], str] = {}  # grupo -> motivo de reprovar limpo
    for m in mutantes:
        linha = {
            "id": m.id,
            "area": m.area,
            "arquivo": m.arquivo.removeprefix("src/"),
            "mudanca": f"`{_resumir(m.de)}` -> `{_resumir(m.para)}`",
        }
        if time.monotonic() - inicio > prazo:
            resultado, teste = "nao rodado", ""
        else:
            resultado, teste = _resultado(m, limpos)
            print(f"{m.id} {resultado:<11} {teste}", flush=True)
        linhas.append({**linha, "resultado": resultado, "teste": teste})
    return linhas


def relatorio(linhas: list[dict[str, str]], segundos: float) -> str:
    def contar(resultado: str) -> int:
        return sum(1 for x in linhas if x["resultado"] == resultado)

    mortos, vivos = contar("morto"), contar("sobreviveu")
    medidos = mortos + vivos
    # Com erro ou mutante nao rodado a medida esta incompleta: sem percentual.
    completo = medidos and not contar("erro") and not contar("nao rodado")
    nota = f"{100 * mortos / medidos:.1f}%" if completo else "n/d"
    saida = [
        f"Mutantes manuais: {mortos} mortos de {medidos} ({nota}); "
        f"{vivos} sobreviveram, {contar('equivalente')} equivalentes ficaram de "
        f"fora da conta, {contar('erro')} com erro e {contar('nao rodado')} nao "
        f"rodados. Duracao: {segundos / 60:.1f} min.",
        "",
        "| Id | Area | Arquivo | Mudanca | Resultado | Teste que falhou |",
        "|---|---|---|---|---|---|",
    ]
    saida += [
        f"| {x['id']} | {x['area']} | `{x['arquivo']}` | {x['mudanca']} | "
        f"{x['resultado']} | `{x['teste']}` |"
        for x in linhas
    ]
    return "\n".join(saida) + "\n"


def main(argv: list[str]) -> int:
    analisador = argparse.ArgumentParser(description="Mutantes manuais (make mutacao)")
    analisador.add_argument("--prazo", type=float, default=PRAZO_PADRAO_SEGUNDOS)
    analisador.add_argument("--so", nargs="*", default=[], help="ids, ex.: D01 R03")
    argumentos = analisador.parse_args(argv)
    plano = carregar()
    todos = plano.mutantes
    problemas = conferir(todos)
    validos = [m.id for m in todos]
    if desconhecidos := [i for i in argumentos.so if i not in validos]:
        problemas.append(f"ids desconhecidos: {' '.join(desconhecidos)}")
        problemas.append(f"ids validos: {' '.join(validos)}")
    if problemas:
        print("\n".join(problemas), file=sys.stderr)
        return 2
    escolhidos = tuple(m for m in todos if not argumentos.so or m.id in argumentos.so)
    inicio = time.monotonic()
    preparar_copia(plano.copiar)
    linhas = executar(escolhidos, argumentos.prazo)
    texto = relatorio(linhas, time.monotonic() - inicio)
    RELATORIO.parent.mkdir(exist_ok=True)
    RELATORIO.write_text(texto, encoding="utf-8")
    print("\n" + texto)
    # Sobrevivente nao reprova (sem gate, ADR-041); erro e medida invalida.
    return 1 if any(x["resultado"] == "erro" for x in linhas) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
