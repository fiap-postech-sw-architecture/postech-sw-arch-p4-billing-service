"""``entrypoint.sh``: o comando que cada processo da imagem executa."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[2] / "entrypoint.sh"


def _executado(tmp_path: Path, processo: str, **env: str) -> list[str]:
    """O programa e os argumentos que o entrypoint executa, por ``uvicorn`` e
    ``python`` falsos que so os imprimem (sem o banner da primeira linha)."""
    for programa in ("uvicorn", "python"):
        falso = tmp_path / programa
        falso.write_text('#!/bin/sh\nprintf "%s\\n" "${0##*/}" "$@"\n')
        falso.chmod(0o755)
    resultado = subprocess.run(  # noqa: S603 - o entrypoint do proprio repositorio
        ["/bin/bash", str(ENTRYPOINT), processo],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
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
