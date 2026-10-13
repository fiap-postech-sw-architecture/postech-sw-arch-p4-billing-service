"""``entrypoint.sh``: o comando que cada processo da imagem executa."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[2] / "entrypoint.sh"


def _rodar(
    tmp_path: Path, processo: str, **env: str
) -> subprocess.CompletedProcess[str]:
    """Roda o entrypoint com ``uvicorn`` e ``python`` falsos que so imprimem o
    programa e os argumentos."""
    for programa in ("uvicorn", "python"):
        falso = tmp_path / programa
        falso.write_text('#!/bin/sh\nprintf "%s\\n" "${0##*/}" "$@"\n')
        falso.chmod(0o755)
    return subprocess.run(  # noqa: S603 - o entrypoint do proprio repositorio
        ["/bin/bash", str(ENTRYPOINT), processo],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def _executado(tmp_path: Path, processo: str, **env: str) -> list[str]:
    """O programa e os argumentos que o entrypoint executa (sem o banner da
    primeira linha)."""
    resultado = _rodar(tmp_path, processo, **env)
    assert resultado.returncode == 0, resultado.stderr
    return resultado.stdout.splitlines()[1:]


@pytest.mark.parametrize(
    ("ambiente", "prefixo"),
    [({"ROOT_PATH": "/billing"}, "/billing"), ({}, "")],
    ids=["atras-da-borda", "sem-root-path"],
)
def test_api_sobe_com_o_prefixo_da_borda_no_root_path(
    tmp_path: Path, ambiente: dict[str, str], prefixo: str
) -> None:
    executado = _executado(tmp_path, "api", **ambiente)

    assert executado[:3] == ["uvicorn", "src.main:criar_app", "--factory"]
    assert executado[-2:] == ["--root-path", prefixo]


def test_api_nao_escreve_access_log_do_uvicorn_nem_anuncia_o_servidor(
    tmp_path: Path,
) -> None:
    # O access log estruturado, com request_id, sai do middleware; o do uvicorn
    # e uma linha de texto sem campos.
    executado = _executado(tmp_path, "api")

    assert {"--no-access-log", "--no-server-header", "--no-proxy-headers"} <= set(
        executado
    )


@pytest.mark.parametrize(
    ("processo", "esperado"),
    [
        ("prazos", ["python", "-m", "src.prazos"]),
        ("relay", ["python", "-m", "src.relay"]),
        ("consumidor", ["python", "-m", "src.consumidor"]),
        ("banco", ["python", "-m", "src.banco"]),
        ("aguarda-banco", ["python", "-m", "src.banco", "aguardar"]),
    ],
    ids=["prazos", "relay", "consumidor", "banco", "aguarda-banco"],
)
def test_cada_processo_executa_o_seu_modulo(
    tmp_path: Path, processo: str, esperado: list[str]
) -> None:
    assert _executado(tmp_path, processo) == esperado


def test_processo_desconhecido_sai_com_64_e_lista_os_validos(tmp_path: Path) -> None:
    resultado = _rodar(tmp_path, "worker")

    assert resultado.returncode == 64
    assert "Processo desconhecido: worker" in resultado.stderr
    assert "aguarda-banco" in resultado.stderr
