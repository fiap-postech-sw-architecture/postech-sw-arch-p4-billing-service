"""Scripts do CI (``scripts/ci/``): a conferencia que estreita a excecao do
trivy (KSV-0109) ao ConfigMap do ``replica-set.js`` e o relatorio das imagens
de terceiros, que nunca reprova o job, o registro da saida do pip-audit e do
trivy no summary, o formato das entradas do ``.gitleaksignore``, os nomes das
variaveis do ``make kind-deploy``, a guarda de frescor do ``make diff-cover`` e
o ``dependabot.yml``."""

from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

RAIZ = Path(__file__).resolve().parents[2]
CONFERENCIA = RAIZ / "scripts" / "ci" / "confere-senhas-do-script.sh"
RELATORIO_TERCEIROS = RAIZ / "scripts" / "ci" / "relatorio-trivy-terceiros.sh"
REGISTRA_NO_SUMMARY = RAIZ / "scripts" / "ci" / "registra-no-summary.sh"


def _conferir(arquivo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - o script do proprio repositorio
        ["/bin/bash", str(CONFERENCIA), str(arquivo)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_o_replica_set_do_repositorio_so_le_senha_do_ambiente() -> None:
    resultado = _conferir(RAIZ / "k8s" / "base" / "replica-set.js")

    assert resultado.returncode == 0, resultado.stderr


@pytest.mark.parametrize(
    "linha",
    [
        'pwd: "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a6978",',
        "pwd: process.env.MONGO_BILLING_PASSWORD,",
        'password: "senha-literal",',
        "pwd: segredo(usuario.senha) + 'x',",
        "secret: 'abc'",
        'token: "ghp_exemplo"',
        '"pwd": "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a6978",',
        "'pwd': 'senha-literal',",
        '["pwd"]: "senha-literal",',
        'pwd: segredo("senha-literal"),',
    ],
    ids=[
        "pwd-literal",
        "pwd-fora-do-segredo",
        "password-literal",
        "segredo-concatenado-a-literal",
        "secret-literal",
        "token-literal",
        "chave-entre-aspas-duplas",
        "chave-entre-aspas-simples",
        "chave-computada",
        "segredo-com-literal",
    ],
)
def test_campo_de_senha_com_valor_que_nao_vem_do_segredo_reprova(
    tmp_path: Path, linha: str
) -> None:
    script = tmp_path / "replica-set.js"
    script.write_text(f"usuario.banco.createUser({{\n  {linha}\n}});\n")

    resultado = _conferir(script)

    assert resultado.returncode == 1
    assert linha in resultado.stderr


@pytest.mark.parametrize(
    "campo", ["pwd: segredo(usuario.senha),", '"pwd": segredo(usuario.senha),']
)
def test_campo_de_senha_com_a_chamada_segredo_passa(tmp_path: Path, campo: str) -> None:
    script = tmp_path / "replica-set.js"
    script.write_text(f"createUser({{\n  {campo}\n}});\n")

    assert _conferir(script).returncode == 0


def test_script_sem_campo_de_senha_reprova_porque_nao_prova_nada(
    tmp_path: Path,
) -> None:
    script = tmp_path / "replica-set.js"
    script.write_text("print('sem usuarios');\n")

    resultado = _conferir(script)

    assert resultado.returncode == 1
    assert "nenhum campo de senha" in resultado.stderr


# O trivy de mentira repete o do CI: o relatorio sai no stdout e o codigo e 1
# quando ha achado, salvo com --exit-code 0; a imagem de FALHA_EM nao chega a
# ser varrida (pull recusado), e o comando sai com 1 mesmo com --exit-code 0.
TRIVY_FALSO = """#!/usr/bin/env bash
echo "trivy $*" >> "$TRIVY_LOG"
imagem="${*: -1}"
if [ -n "$TRIVY_FALHA_EM" ] && [[ "$imagem" == *"$TRIVY_FALHA_EM"* ]]; then
  echo "FATAL pull recusado: $imagem" >&2
  exit 1
fi
echo "RELATORIO $imagem: Total: 3 (HIGH: 3)"
case " $* " in *" --exit-code 0 "*) exit 0 ;; esac
exit 1
"""


def _relatar(
    tmp_path: Path, imagens: list[str], falha_em: str = ""
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """Roda o relatorio sobre um diretorio de manifests com ``imagens`` e o
    trivy de mentira; devolve o processo, o summary e o log de chamadas."""
    manifestos = tmp_path / "manifestos"
    manifestos.mkdir()
    linhas = "".join(
        f"        - name: c{i}\n          image: {imagem}\n"
        for i, imagem in enumerate(imagens)
    )
    (manifestos / "deployment.yaml").write_text(f"containers:\n{linhas}")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "trivy").write_text(TRIVY_FALSO)
    (bin_ / "trivy").chmod(0o755)
    resumo, log = tmp_path / "resumo.md", tmp_path / "trivy.log"
    resultado = subprocess.run(  # noqa: S603 - o script do proprio repositorio
        ["/bin/bash", str(RELATORIO_TERCEIROS), str(manifestos)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={
            **os.environ,
            "PATH": f"{bin_}:{os.environ['PATH']}",
            "GITHUB_STEP_SUMMARY": str(resumo),
            "TRIVY_LOG": str(log),
            "TRIVY_FALHA_EM": falha_em,
        },
    )
    return (
        resultado,
        resumo.read_text() if resumo.exists() else "",
        log.read_text() if log.exists() else "",
    )


def test_achado_do_trivy_nao_reprova_e_entra_no_relatorio(tmp_path: Path) -> None:
    resultado, resumo, chamadas = _relatar(
        tmp_path, ["pytstop-billing-service", "terceiro/a:1", "terceiro/b:2"]
    )

    assert resultado.returncode == 0, resultado.stderr
    assert "RELATORIO terceiro/a:1" in resumo
    assert "RELATORIO terceiro/b:2" in resumo
    assert "falhou" not in resumo
    assert "pytstop-billing-service" not in chamadas


def test_falha_da_varredura_vira_aviso_e_nao_reprova(tmp_path: Path) -> None:
    resultado, resumo, _ = _relatar(
        tmp_path, ["terceiro/a:1", "terceiro/b:2"], falha_em="terceiro/a"
    )

    assert resultado.returncode == 0, resultado.stderr
    assert "terceiro/a:1: varredura falhou (codigo 1)" in resumo
    assert "::warning::" in resultado.stdout
    # A falha de uma imagem nao impede o relatorio da seguinte.
    assert "RELATORIO terceiro/b:2" in resumo


def test_todas_as_varreduras_falhando_tambem_nao_reprova(tmp_path: Path) -> None:
    resultado, resumo, _ = _relatar(
        tmp_path, ["terceiro/a:1", "terceiro/b:2"], falha_em="terceiro"
    )

    assert resultado.returncode == 0, resultado.stderr
    assert resumo.count("varredura falhou") == 2


def test_sem_imagem_de_terceiro_reprova_porque_o_relatorio_nao_prova_nada(
    tmp_path: Path,
) -> None:
    resultado, resumo, chamadas = _relatar(tmp_path, ["pytstop-billing-service"])

    assert resultado.returncode == 1
    assert "nenhuma imagem de terceiro" in resultado.stderr
    assert resumo == chamadas == ""


def _registrar(
    tmp_path: Path, *comando: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    resumo = tmp_path / "resumo.md"
    resultado = subprocess.run(  # noqa: S603 - o script do proprio repositorio
        ["/bin/bash", str(REGISTRA_NO_SUMMARY), "pip-audit", *comando],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(resumo)},
    )
    return resultado, resumo.read_text() if resumo.exists() else ""


def test_comando_que_passa_vai_para_o_log_e_para_o_summary(tmp_path: Path) -> None:
    resultado, resumo = _registrar(tmp_path, "echo", "No known vulnerabilities found")

    assert resultado.returncode == 0
    assert resultado.stdout == "No known vulnerabilities found\n"
    assert resumo == (
        "### pip-audit: sem achado\n```text\nNo known vulnerabilities found\n```\n"
    )


def test_comando_que_reprova_registra_a_saida_e_mantem_o_codigo(
    tmp_path: Path,
) -> None:
    resultado, resumo = _registrar(
        tmp_path, "/bin/sh", "-c", "echo achado-no-stdout; echo erro >&2; exit 3"
    )

    assert resultado.returncode == 3
    assert "achado-no-stdout" in resultado.stdout
    assert resumo.startswith("### pip-audit: reprovou (codigo 3)\n```text\n")
    assert "achado-no-stdout" in resumo
    assert "erro" in resumo


def test_a_saida_aparece_no_log_enquanto_o_comando_roda(tmp_path: Path) -> None:
    """Com o comando ainda de pe (ele espera um arquivo que so o teste cria), a
    primeira linha ja tem de estar no log: trivy baixando o banco ou um
    pip-audit pendurado precisam mostrar o que fazem ao vivo."""
    fim = tmp_path / "fim"
    espera = f"echo comecou; until [ -e {fim} ]; do sleep 0.1; done; echo terminou"
    processo = subprocess.Popen(  # noqa: S603 - o script do proprio repositorio
        ["/bin/bash", str(REGISTRA_NO_SUMMARY), "ao vivo", "/bin/sh", "-c", espera],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(tmp_path / "resumo.md")},
    )
    assert processo.stdout is not None
    try:
        pronto, _, _ = select.select([processo.stdout], [], [], 10)
        primeira = processo.stdout.readline() if pronto else ""
        fim.touch()
        resto, _ = processo.communicate(timeout=10)
    finally:
        processo.kill()

    assert primeira == "comecou\n"
    assert resto == "terminou\n"
    assert processo.returncode == 0


def test_comando_inexistente_reprova_e_aparece_no_summary(tmp_path: Path) -> None:
    resultado, resumo = _registrar(tmp_path, "comando-que-nao-existe-aqui")

    assert resultado.returncode == 127
    assert "reprovou (codigo 127)" in resumo


def test_sem_comando_o_script_recusa(tmp_path: Path) -> None:
    resultado, resumo = _registrar(tmp_path)

    assert resultado.returncode == 64
    assert "uso:" in resultado.stderr
    assert resumo == ""


@pytest.mark.parametrize(
    ("job", "ferramenta", "titulo"),
    [
        ("pip-audit", "uvx pip-audit", "dependencias de runtime"),
        ("trivy", "trivy image", "HIGH e CRITICAL com correcao"),
    ],
)
def test_o_security_yml_registra_o_pip_audit_e_o_trivy_no_summary(
    job: str, ferramenta: str, titulo: str
) -> None:
    """O titulo do trivy diz o filtro do passo: sem ele, "sem achado" leria como
    "sem CVE nenhuma"."""
    fluxo = yaml.safe_load(
        (RAIZ / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
    )
    comandos = [
        " ".join(passo["run"].split())
        for passo in fluxo["jobs"][job]["steps"]
        if ferramenta in passo.get("run", "")
    ]

    assert len(comandos) == 1
    assert comandos[0].startswith("scripts/ci/registra-no-summary.sh ")
    assert titulo in comandos[0].split(ferramenta)[0]


def test_dependabot_tem_agenda_prefixo_grupos_e_ignore_de_cada_bloco() -> None:
    config = yaml.safe_load(
        (RAIZ / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    )
    blocos = {bloco["package-ecosystem"]: bloco for bloco in config["updates"]}
    agenda = {
        "interval": "weekly",
        "day": "monday",
        "time": "06:00",
        "timezone": "America/Sao_Paulo",
    }
    patch_e_minor = {"update-types": ["minor", "patch"]}
    # O Python sobe a mao; o uv do builder (um FROM) sobe pelo Dependabot, sem ignore.
    so_patch_do_python = [
        {
            "dependency-name": "python",
            "update-types": [
                "version-update:semver-major",
                "version-update:semver-minor",
            ],
        }
    ]
    # Um grupo por nome (o do OpenTelemetry) ficaria vazio: o Dependabot prefere
    # o de update-types, entao cada bloco tem so o de patch e minor.
    esperado = {
        "uv": ("chore(deps)", {"python-patch-e-minor": patch_e_minor}, []),
        "github-actions": ("ci", {"actions-patch-e-minor": patch_e_minor}, []),
        "docker": (
            "chore(deps)",
            {"docker-patch-e-minor": patch_e_minor},
            so_patch_do_python,
        ),
    }

    assert set(blocos) == set(esperado)
    for nome, (prefixo, grupos, ignore) in esperado.items():
        bloco = blocos[nome]
        assert bloco["directory"] == "/", nome
        assert bloco["schedule"] == agenda, nome
        assert bloco["open-pull-requests-limit"] == 3, nome
        assert bloco["commit-message"] == {"prefix": prefixo}, nome
        assert bloco["groups"] == grupos, nome
        assert bloco.get("ignore", []) == ignore, nome
        # Rotulo declarado e inexistente no repositorio vira comentario em cada PR.
        assert "labels" not in bloco, nome


PADRAO = "../postech-sw-arch-p4-platform"


@pytest.mark.parametrize(
    ("variaveis", "platform", "overlay"),
    [
        ([], PADRAO, "kind"),
        (["KIND_OVERLAY=kind-ci"], PADRAO, "kind-ci"),
        (["OVERLAY=k3s"], PADRAO, "k3s"),
        (["OVERLAY=k3s", "KIND_OVERLAY=kind-ci"], PADRAO, "kind-ci"),
        (["PLATFORM_DIR=/clone/platform"], "/clone/platform", "kind"),
    ],
    ids=[
        "padroes",
        "kind-overlay",
        "nome-antigo",
        "novo-vence-o-antigo",
        "platform-dir",
    ],
)
def test_kind_deploy_aceita_platform_dir_e_kind_overlay(
    variaveis: list[str], platform: str, overlay: str
) -> None:
    """``PLATFORM_DIR`` e ``KIND_OVERLAY`` sao os nomes do Execution Service;
    ``OVERLAY``, o nome antigo, segue valendo quando ``KIND_OVERLAY`` nao e
    dado."""
    # Sem as variaveis do desenvolvedor (nem as que o make pai passa em
    # MAKEFLAGS, como em `make check OVERLAY=k3s`), e como make filho
    # (MAKELEVEL), que e como o CI roda: o make 4 avisa o diretorio.
    ambiente = {
        chave: valor
        for chave, valor in os.environ.items()
        if chave not in {"PLATFORM_DIR", "KIND_OVERLAY", "OVERLAY", "MAKEFLAGS"}
    }
    resultado = subprocess.run(  # noqa: S603 - o Makefile do proprio repositorio
        ["make", "-n", "--no-print-directory", "kind-deploy", *variaveis],  # noqa: S607
        cwd=RAIZ,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        env={**ambiente, "MAKELEVEL": "1"},
    )

    assert resultado.returncode == 0, resultado.stderr
    comando = " ".join(resultado.stdout.replace("\\\n", " ").split())
    assert comando.startswith(f"{platform}/scripts/ci/implantar-servicos.sh")
    assert f" --overlay {overlay} billing-service=" in comando


def _diff_cover(
    tmp_path: Path, xml_mais_novo: bool
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """``make diff-cover`` numa arvore minuscula, com um ``uvx`` falso no PATH que
    so registra os argumentos."""
    shutil.copy(RAIZ / "Makefile", tmp_path / "Makefile")
    (tmp_path / "src").mkdir()
    codigo, xml = tmp_path / "src" / "modulo.py", tmp_path / "coverage.xml"
    codigo.write_text("X = 1\n", encoding="utf-8")
    xml.write_text("<coverage/>", encoding="utf-8")
    os.utime(codigo, (2_000_000_000, 2_000_000_000))
    do_xml = 2_000_000_100 if xml_mais_novo else 1_999_999_900
    os.utime(xml, (do_xml, do_xml))
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    argumentos = tmp_path / "argumentos-do-uvx"
    falso = bin_ / "uvx"
    falso.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > {argumentos}\n', encoding="utf-8"
    )
    falso.chmod(0o755)
    ambiente = {
        chave: valor for chave, valor in os.environ.items() if chave != "MAKEFLAGS"
    }
    resultado = subprocess.run(
        ["make", "--no-print-directory", "diff-cover"],  # noqa: S607
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        env={**ambiente, "PATH": f"{bin_}:{ambiente['PATH']}", "MAKELEVEL": "1"},
    )
    return resultado, argumentos


def test_diff_cover_recusa_coverage_xml_mais_velho_que_o_codigo(
    tmp_path: Path,
) -> None:
    resultado, argumentos = _diff_cover(tmp_path, xml_mais_novo=False)

    assert resultado.returncode != 0
    assert "coverage.xml is older than src/, run make test first" in resultado.stderr
    assert not argumentos.exists()


def test_diff_cover_com_xml_fresco_mede_tambem_o_arquivo_novo(
    tmp_path: Path,
) -> None:
    resultado, argumentos = _diff_cover(tmp_path, xml_mais_novo=True)

    assert resultado.returncode == 0, resultado.stderr
    assert argumentos.read_text(encoding="utf-8").split() == [
        "diff-cover==10.6.0",
        "coverage.xml",
        "--branch-coverage",
        "--include-untracked",
        "--compare-branch=origin/main",
        "--fail-under=90",
        "--format",
        "markdown:reports/diff-cover.md",
    ]


# Fingerprint do gitleaks no modo git: commit:arquivo:regra:linha. Linha fora
# desse formato nao esconde achado nenhum, e o job volta a reprovar.
FINGERPRINT = re.compile(r"^[0-9a-f]{40}:[^:]+:[A-Za-z0-9._-]+:[0-9]+$")


@pytest.mark.parametrize(
    ("linha", "valida"),
    [
        (
            "e382e8f28ef61b90a6606107835139a2239adaed:tests/a.py:generic-api-key:17",
            True,
        ),
        ("6c00a8045c13b37af3a1f7caff3d480b60fc9f58:k8s/b.yaml:pytstop-url-x:606", True),
        ("e382e8f:tests/a.py:generic-api-key:17", False),
        ("e382e8f28ef61b90a6606107835139a2239adaed:tests/a.py:generic-api-key", False),
        (
            "e382e8f28ef61b90a6606107835139a2239adaed:tests/a.py:generic-api-key:x",
            False,
        ),
        (
            "E382E8F28EF61B90A6606107835139A2239ADAED:tests/a.py:generic-api-key:17",
            False,
        ),
    ],
    ids=[
        "valida",
        "valida-com-hifen",
        "sha-curto",
        "sem-linha",
        "linha-nao-numero",
        "sha-em-maiuscula",
    ],
)
def test_formato_do_fingerprint_do_gitleaks(linha: str, valida: bool) -> None:
    assert bool(FINGERPRINT.match(linha)) is valida


def test_toda_entrada_do_gitleaksignore_tem_o_formato_do_fingerprint() -> None:
    arquivo = RAIZ / ".gitleaksignore"
    linhas = (
        arquivo.read_text(encoding="utf-8").splitlines() if arquivo.exists() else []
    )
    entradas = [x for x in linhas if x.strip() and not x.lstrip().startswith("#")]

    assert [x for x in entradas if not FINGERPRINT.match(x)] == []
