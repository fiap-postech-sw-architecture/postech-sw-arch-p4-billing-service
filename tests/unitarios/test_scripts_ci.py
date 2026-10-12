"""``scripts/ci/confere-senhas-do-script.sh``: a conferencia que estreita a
excecao do trivy (KSV-0109) ao ConfigMap do ``replica-set.js``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
CONFERENCIA = RAIZ / "scripts" / "ci" / "confere-senhas-do-script.sh"


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
    ],
    ids=[
        "pwd-literal",
        "pwd-fora-do-segredo",
        "password-literal",
        "segredo-concatenado-a-literal",
        "secret-literal",
        "token-literal",
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


def test_campo_de_senha_com_a_chamada_segredo_passa(tmp_path: Path) -> None:
    script = tmp_path / "replica-set.js"
    script.write_text("createUser({\n  pwd: segredo(usuario.senha),\n});\n")

    assert _conferir(script).returncode == 0


def test_script_sem_campo_de_senha_reprova_porque_nao_prova_nada(
    tmp_path: Path,
) -> None:
    script = tmp_path / "replica-set.js"
    script.write_text("print('sem usuarios');\n")

    resultado = _conferir(script)

    assert resultado.returncode == 1
    assert "nenhum campo de senha" in resultado.stderr
