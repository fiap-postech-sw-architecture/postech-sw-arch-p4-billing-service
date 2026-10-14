"""Scripts do CI (``scripts/ci/``): a conferencia que estreita a excecao do
trivy (KSV-0109) ao ConfigMap do ``replica-set.js`` e o relatorio das imagens
de terceiros, que nunca reprova o job, e os nomes das variaveis do
``make kind-deploy``."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
CONFERENCIA = RAIZ / "scripts" / "ci" / "confere-senhas-do-script.sh"
RELATORIO_TERCEIROS = RAIZ / "scripts" / "ci" / "relatorio-trivy-terceiros.sh"


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
    """Os tres servicos usam ``PLATFORM_DIR`` e ``KIND_OVERLAY``; ``OVERLAY``,
    o nome antigo, segue valendo quando ``KIND_OVERLAY`` nao e dado."""
    resultado = subprocess.run(  # noqa: S603 - o Makefile do proprio repositorio
        ["make", "-n", "kind-deploy", *variaveis],  # noqa: S607
        cwd=RAIZ,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert resultado.returncode == 0, resultado.stderr
    comando = " ".join(resultado.stdout.replace("\\\n", " ").split())
    assert comando.startswith(f"{platform}/scripts/ci/implantar-servicos.sh")
    assert f" --overlay {overlay} billing-service=" in comando
