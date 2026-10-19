"""Contrato local do caller CD; imagem e deploy são testados no Platform."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

RAIZ = Path(__file__).resolve().parents[2]
WORKFLOWS = RAIZ / ".github" / "workflows"
CD = WORKFLOWS / "cd.yml"
SHA_PADRAO = re.compile(r"^[0-9a-f]{40}$")
CD_REUTILIZAVEL = (
    "fiap-postech-sw-architecture/postech-sw-arch-p4-platform/"
    ".github/workflows/cd-servico.yml"
)


def _carrega(caminho: Path = CD) -> dict[Any, Any]:
    carregado: dict[Any, Any] = yaml.safe_load(caminho.read_text())
    return carregado


def _gatilhos(workflow: dict[Any, Any]) -> dict[str, Any]:
    # O YAML 1.1 do PyYAML le a chave `on` como o booleano True.
    gatilhos: dict[str, Any] = workflow[True]
    return gatilhos


def _jobs() -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = _carrega()["jobs"]
    return jobs


def test_caller_preserva_gates_locais_antes_do_cd_reutilizavel() -> None:
    jobs = _jobs()

    assert list(jobs) == ["ci", "security-scan", "cd"]
    assert jobs["ci"] == {
        "uses": "./.github/workflows/ci.yml",
        "permissions": {"contents": "read"},
    }
    assert jobs["security-scan"] == {
        "uses": "./.github/workflows/security.yml",
        "permissions": {"contents": "read"},
    }
    assert set(jobs["cd"]) == {"needs", "uses", "permissions", "with"}
    assert jobs["cd"]["needs"] == ["ci", "security-scan"]
    assert re.fullmatch(
        re.escape(CD_REUTILIZAVEL) + r"@[0-9a-f]{40}", jobs["cd"]["uses"]
    )


def test_caller_encaminha_servico_e_os_quatro_inputs_sem_transformacao() -> None:
    entradas = _gatilhos(_carrega())["workflow_dispatch"]["inputs"]

    assert _jobs()["cd"]["with"] == {
        "servico": "billing-service",
        **{nome: "${{ inputs." + nome + " }}" for nome in entradas},
    }
    assert "env" not in _carrega()


def test_caller_concede_so_a_uniao_das_permissoes_do_cd() -> None:
    assert _carrega()["permissions"] == {"contents": "read"}
    assert _jobs()["cd"]["permissions"] == {
        "contents": "read",
        "actions": "read",
        "packages": "write",
    }
    # Sem secrets: inherit, environment ou passos locais com acesso a segredos.
    assert set(_carrega()) == {"name", True, "permissions", "concurrency", "jobs"}


def test_ci_e_seguranca_conservam_os_nove_checks_e_workflow_call() -> None:
    for arquivo, checks in [
        ("ci.yml", {"lint", "type-check", "security", "test", "sonarqube", "build"}),
        ("security.yml", {"pip-audit", "gitleaks", "trivy"}),
    ]:
        workflow = _carrega(WORKFLOWS / arquivo)
        assert set(workflow["jobs"]) == checks
        assert "workflow_call" in _gatilhos(workflow)


def test_gatilhos_sao_o_push_na_main_e_o_dispatch() -> None:
    gatilhos = _gatilhos(_carrega())

    assert set(gatilhos) == {"push", "workflow_dispatch"}
    assert gatilhos["push"] == {"branches": ["main"]}
    entradas = gatilhos["workflow_dispatch"]["inputs"]
    assert list(entradas) == [
        "sha_platform",
        "sha_os_service",
        "sha_billing_service",
        "sha_execution_service",
    ]
    assert all(
        entrada["required"] is False and entrada["type"] == "string"
        for entrada in entradas.values()
    )


def test_concurrency_por_repositorio_sem_cancelar_o_run_em_curso() -> None:
    assert _carrega()["concurrency"] == {
        "group": "cd-${{ github.repository }}",
        "cancel-in-progress": False,
    }
