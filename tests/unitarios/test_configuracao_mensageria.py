"""Configuracao do relay e do consumidor e o arquivo de vida dos processos."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from src.compartilhado.infraestrutura.processo import CONECTANDO, PRONTO, sinalizar
from src.configuracao import (
    Configuracao,
    ConfiguracaoDoConsumidor,
    ConfiguracaoDoRelay,
    ConfiguracaoDosComandos,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

URL = "amqp://billing:senha-de-teste@rabbitmq:5672/%2F"  # gitleaks:allow (teste)
BASE = {
    "ENVIRONMENT": "test",
    "MP_MODE": "simulado",
    "RABBITMQ_URL": URL,
}


def _env(**extra: str) -> Mapping[str, str]:
    return {**BASE, **extra}


class TestRelay:
    def test_padroes(self) -> None:
        config = ConfiguracaoDoRelay.do_ambiente(_env())
        assert config.rabbitmq_usuario == "billing"
        assert config.heartbeat == Path("/tmp/relay-heartbeat")  # noqa: S108 - padrao do container
        assert config.porta_metricas == 9100
        assert config.banco.mongodb_banco == "billing"
        assert "senha-de-teste" not in repr(config)

    def test_heartbeat_e_porta_pelo_ambiente(self) -> None:
        config = ConfiguracaoDoRelay.do_ambiente(
            _env(RELAY_HEARTBEAT="/tmp/x", METRICS_PORT="9200")  # noqa: S108
        )
        assert (config.heartbeat, config.porta_metricas) == (Path("/tmp/x"), 9200)  # noqa: S108

    @pytest.mark.parametrize(
        ("url", "erro"),
        [
            ("", "amqp"),
            ("http://billing:x@rabbitmq:5672/", "amqp"),
            ("amqp://:5672/", "amqp"),
            ("amqp://rabbitmq:5672/", "usuario do servico"),
        ],
        ids=["ausente", "esquema", "sem-host", "sem-usuario"],
    )
    def test_rabbitmq_url_invalida_derruba_o_boot(self, url: str, erro: str) -> None:
        with pytest.raises(ValueError, match=erro):
            ConfiguracaoDoRelay.do_ambiente(_env(RABBITMQ_URL=url))

    def test_usuario_com_caracter_escapado(self) -> None:
        config = ConfiguracaoDoRelay.do_ambiente(
            _env(RABBITMQ_URL="amqps://bil%40ling:x@rabbitmq:5671/")  # gitleaks:allow
        )
        assert config.rabbitmq_usuario == "bil@ling"


class TestConsumidor:
    def test_le_o_que_os_comandos_usam_sem_o_jwks(self) -> None:
        config = ConfiguracaoDoConsumidor.do_ambiente(
            _env(
                BILLING_PUBLIC_URL="http://billing.teste", ORCAMENTO_VALIDADE_HORAS="2"
            )
        )
        assert config.heartbeat == Path("/tmp/consumidor-heartbeat")  # noqa: S108
        assert config.comandos.url_publica == "http://billing.teste"
        assert config.comandos.orcamento_validade.total_seconds() == 7200
        assert config.comandos.mp_notification_url == (
            "http://billing.teste/api/v1/webhooks/mercadopago"
        )
        assert "senha-de-teste" not in repr(config)

    def test_producao_exige_segredo_do_link_proprio(self) -> None:
        with pytest.raises(ValueError, match="ORCAMENTO_LINK_SECRET"):
            ConfiguracaoDoConsumidor.do_ambiente(
                _env(
                    ENVIRONMENT="production",
                    SIMULADOR_PERMITIDO="true",
                    BILLING_PUBLIC_URL="https://billing.teste",
                    MONGODB_URI="mongodb://mongo:27017",
                )
            )


def test_api_e_consumidor_leem_os_comandos_igual() -> None:
    env = _env(JWKS_URL="http://os.teste/.well-known/jwks.json")
    assert Configuracao.do_ambiente(env).comandos == (
        ConfiguracaoDosComandos.do_ambiente(env)
    )


def test_arquivo_de_vida_diz_se_esta_pronto(tmp_path: Path) -> None:
    arquivo = tmp_path / "heartbeat"
    sinalizar(arquivo, pronto=False)
    assert arquivo.read_text() == CONECTANDO
    sinalizar(arquivo, pronto=True)
    assert arquivo.read_text() == PRONTO


def test_processos_da_mensageria_nao_carregam_a_pilha_http() -> None:
    # Relay e consumidor nao servem HTTP de negocio: a fabrica do provedor e o
    # caminho do link de decisao vem de modulos sem FastAPI.
    codigo = (
        "import sys, src.consumidor, src.relay; "
        "print(sorted(m for m in ('fastapi', 'starlette', 'src.main') "
        "if m in sys.modules))"
    )
    saida = subprocess.run(  # noqa: S603 - o interpretador do proprio teste
        [sys.executable, "-c", codigo], capture_output=True, text=True, check=True
    )
    assert saida.stdout.strip() == "[]"


def test_erro_de_rabbitmq_url_nao_ecoa_a_senha() -> None:
    url = "http://billing:SENHA-DO-BROKER@rabbitmq:5672/"  # gitleaks:allow (teste)
    with pytest.raises(ValueError, match="amqp") as erro:
        ConfiguracaoDoRelay.do_ambiente(_env(RABBITMQ_URL=url))
    assert "SENHA-DO-BROKER" not in str(erro.value)
