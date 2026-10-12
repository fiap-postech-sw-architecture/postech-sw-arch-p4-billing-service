"""``k8s/``: o ConfigMap dos processos contra o codigo que o le.

Uma variavel renomeada no codigo passa no ``make manifests`` (o kubeconform so
confere o schema) e so cairia no deploy: estes testes leem os literais do
ConfigMap sem o kubectl e sobem as configuracoes com eles.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

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
