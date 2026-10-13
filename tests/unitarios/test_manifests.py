"""``k8s/``: o ConfigMap dos processos contra o codigo que o le.

Uma variavel renomeada no codigo passa no ``make manifests`` (o kubeconform so
confere o schema) e so cairia no deploy: estes testes leem os literais do
ConfigMap sem o kubectl e sobem as configuracoes com eles.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.configuracao import (
    Configuracao,
    ConfiguracaoDoBanco,
    ConfiguracaoDoConsumidor,
    ConfiguracaoDoRelay,
    ConfiguracaoDosPrazos,
    ModoMercadoPago,
)

RAIZ = Path(__file__).resolve().parents[2]
K8S = RAIZ / "k8s"

# O que os Secrets entregam ao pod (secretKeyRef e a MONGODB_URI montada no
# pod); os valores sao de mentira.
SEGREDOS = {
    "MONGODB_URI": "mongodb://billing:x@billing-mongo-0.billing-mongo:27017/billing",
    "ORCAMENTO_LINK_SECRET": "a" * 64,
    "RABBITMQ_URL": "amqp://billing:x@rabbitmq.pytstop-plataforma:5672/%2F",
}


def _documentos_do_base() -> list[dict[str, Any]]:
    """Os documentos YAML dos manifests de ``k8s/base``, fora a kustomization."""
    return [
        documento
        for arquivo in sorted((K8S / "base").glob("*.yaml"))
        if arquivo.name != "kustomization.yaml"
        for documento in yaml.safe_load_all(arquivo.read_text())
        if documento
    ]


def _containers(documento: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """(secao, nome, container) dos init e dos containers do pod do documento."""
    pod = (documento.get("spec") or {}).get("template", {}).get("spec", {})
    for secao in ("initContainers", "containers"):
        for container in pod.get(secao, []):
            yield secao, container["name"], container


def _literais(kustomization: Path) -> dict[str, str]:
    """Os literais do ConfigMap ``billing-service`` de uma kustomization."""
    documento = yaml.safe_load(kustomization.read_text())
    gerador = next(
        g for g in documento["configMapGenerator"] if g["name"] == "billing-service"
    )
    return dict(item.split("=", 1) for item in gerador["literals"])


@pytest.fixture(params=["kind", "kind-ci"])
def ambiente(request: pytest.FixtureRequest) -> dict[str, str]:
    """O ambiente de um pod: o ConfigMap do overlay (o ``kind-ci`` mescla o dele
    sobre o do base) e os segredos."""
    literais = _literais(K8S / "base" / "kustomization.yaml")
    if request.param == "kind-ci":
        literais |= _literais(K8S / "overlays" / "kind-ci" / "kustomization.yaml")
    return {**literais, **SEGREDOS}


def test_o_configmap_sobe_as_cinco_configuracoes_em_producao(
    ambiente: dict[str, str],
) -> None:
    assert ambiente["ENVIRONMENT"] == "production"

    for configuracao in (
        Configuracao,
        ConfiguracaoDoBanco,
        ConfiguracaoDosPrazos,
        ConfiguracaoDoRelay,
        ConfiguracaoDoConsumidor,
    ):
        assert configuracao.do_ambiente(ambiente) is not None


def test_os_valores_do_configmap_chegam_as_configuracoes(
    ambiente: dict[str, str],
) -> None:
    api = Configuracao.do_ambiente(ambiente)
    prazos = ConfiguracaoDosPrazos.do_ambiente(ambiente)
    relay = ConfiguracaoDoRelay.do_ambiente(ambiente)

    assert api.url_publica == ambiente["BILLING_PUBLIC_URL"]
    assert api.jwks_url == ambiente["JWKS_URL"]
    assert api.mongodb_banco == ambiente["MONGODB_DB"]
    assert api.mp_modo is ModoMercadoPago(ambiente["MP_MODE"])
    assert api.mp_timeout_segundos == float(ambiente["MP_TIMEOUT_SEGUNDOS"])
    assert api.orcamento_validade == timedelta(
        hours=float(ambiente["ORCAMENTO_VALIDADE_HORAS"])
    )
    assert api.pagamento_validade == timedelta(
        minutes=float(ambiente["PAGAMENTO_VALIDADE_MINUTOS"])
    )
    assert api.pagamento_max_recusas == int(ambiente["PAGAMENTO_MAX_RECUSAS"])
    assert prazos.intervalo_segundos == float(ambiente["PRAZOS_INTERVALO_SEGUNDOS"])
    assert prazos.pagamento_max_recusas == int(ambiente["PAGAMENTO_MAX_RECUSAS"])
    assert relay.porta_metricas == int(ambiente["METRICS_PORT"])


def test_toda_variavel_do_configmap_e_lida_pelo_codigo() -> None:
    codigo = "\n".join(p.read_text() for p in (RAIZ / "src").rglob("*.py"))
    entrypoint = (RAIZ / "entrypoint.sh").read_text()

    sem_leitor = [
        variavel
        for variavel in _literais(K8S / "base" / "kustomization.yaml")
        if f'"{variavel}"' not in codigo and "${" + variavel not in entrypoint
    ]

    assert sem_leitor == []


def test_root_path_do_configmap_e_o_prefixo_da_borda(ambiente: dict[str, str]) -> None:
    prefixos = {
        "/" + caminho["path"].split("/")[1]
        for documento in _documentos_do_base()
        if documento["kind"] == "Ingress"
        for regra in documento["spec"]["rules"]
        for caminho in regra["http"]["paths"]
    }

    assert prefixos == {"/billing"}
    assert ambiente["ROOT_PATH"] == "/billing"


def test_metrics_port_do_configmap_e_a_porta_que_o_prometheus_raspa(
    ambiente: dict[str, str],
) -> None:
    porta = int(ambiente["METRICS_PORT"])
    portas = {
        documento["metadata"]["name"]: (
            ponto["containerPort"],
            int(
                documento["spec"]["template"]["metadata"]["annotations"][
                    "prometheus.io/port"
                ]
            ),
        )
        for documento in _documentos_do_base()
        if documento["kind"] == "Deployment"
        for _, _, container in _containers(documento)
        for ponto in container.get("ports", [])
        if ponto["name"] == "metrics"
    }

    assert sorted(portas) == [
        "billing-service-consumidor",
        "billing-service-prazos",
        "billing-service-relay",
    ]
    assert set(portas.values()) == {(porta, porta)}
