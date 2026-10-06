from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from src.configuracao import SEGREDO_LINK_DEMO, Configuracao, ModoMercadoPago
from src.main import criar_gateway
from src.pagamento.aplicacao.ports import GatewayPagamentoRecusouError
from src.pagamento.dominio.pagamento import StatusPagamento
from src.pagamento.infraestrutura.mercadopago import MercadoPagoGateway
from src.pagamento.infraestrutura.simulado import GatewayPagamentoSimulado
from tests.factories import AGORA, dinheiro

SEGREDO_FORTE = "3f1c0e5b8a7d4c2e9f6b1a0d8c7e5f4a"


class TestGatewaySimulado:
    @pytest.fixture
    def simulado(self) -> GatewayPagamentoSimulado:
        return GatewayPagamentoSimulado(url_publica="http://billing.teste/")

    def test_cobranca_aponta_para_o_checkout_do_simulador(
        self, simulado: GatewayPagamentoSimulado
    ) -> None:
        pagamento_id = uuid4()
        cobranca = simulado.criar_cobranca(
            pagamento_id=pagamento_id, itens=[], expira_em=AGORA
        )
        assert cobranca.referencia == f"sim-pref-{pagamento_id}"
        assert (
            cobranca.checkout_url
            == f"http://billing.teste/simulador/checkout/{pagamento_id}"
        )

    @pytest.mark.parametrize(
        ("aprovado", "status", "bruto"),
        [
            (True, StatusPagamento.APROVADO, "approved"),
            (False, StatusPagamento.RECUSADO, "rejected"),
        ],
    )
    def test_resultado_simulado_aparece_na_consulta(
        self,
        simulado: GatewayPagamentoSimulado,
        aprovado: bool,
        status: StatusPagamento,
        bruto: str,
    ) -> None:
        pagamento_id = uuid4()
        referencia = simulado.registrar_resultado(
            pagamento_id=pagamento_id, valor=dinheiro("10.00"), aprovado=aprovado
        )
        situacao = simulado.consultar_pagamento(referencia)
        assert situacao is not None
        assert (situacao.status, situacao.status_provedor) == (status, bruto)
        assert situacao.referencia_externa == str(pagamento_id)
        assert situacao.valor == dinheiro("10.00")
        assert simulado.consultar_pagamento("sim-desconhecido") is None

    def test_estorno(self, simulado: GatewayPagamentoSimulado) -> None:
        aprovado = simulado.registrar_resultado(
            pagamento_id=uuid4(), valor=dinheiro("1.00"), aprovado=True
        )
        simulado.estornar(aprovado, chave_idempotencia="k")
        simulado.estornar(aprovado, chave_idempotencia="k")  # idempotente
        situacao = simulado.consultar_pagamento(aprovado)
        assert situacao is not None
        assert situacao.status is StatusPagamento.ESTORNADO
        # Pagamento anterior a um restart do processo: aceito.
        simulado.estornar("sim-perdido", chave_idempotencia="k")

    def test_estorno_de_recusado_e_recusado(
        self, simulado: GatewayPagamentoSimulado
    ) -> None:
        recusado = simulado.registrar_resultado(
            pagamento_id=uuid4(), valor=dinheiro("1.00"), aprovado=False
        )
        with pytest.raises(GatewayPagamentoRecusouError):
            simulado.estornar(recusado, chave_idempotencia="k")


DEV = {"ENVIRONMENT": "development"}


class TestConfiguracao:
    def test_sem_environment_assume_producao(self) -> None:
        with pytest.raises(ValueError, match="ENVIRONMENT=production"):
            Configuracao.do_ambiente({})

    def test_padroes_de_desenvolvimento(self) -> None:
        config = Configuracao.do_ambiente(DEV)
        assert config.ambiente == "development"
        assert config.mongodb_uri.startswith("mongodb://localhost:27017")
        assert config.mongodb_banco == "billing"
        assert config.link_segredo == SEGREDO_LINK_DEMO
        assert config.orcamento_validade == timedelta(hours=72)
        assert config.pagamento_validade == timedelta(minutes=60)
        assert config.mp_modo is ModoMercadoPago.SIMULADO
        assert config.jwt_emissor == "pytstop-os-service"
        assert config.jwt_audiencia == "pytstop"
        assert (
            config.mp_notification_url
            == "http://localhost:8002/api/v1/webhooks/mercadopago"
        )
        assert isinstance(criar_gateway(config), GatewayPagamentoSimulado)

    def test_segredos_fora_do_repr(self) -> None:
        texto = repr(
            Configuracao.do_ambiente(
                {
                    **DEV,
                    "MP_MODE": "mercadopago",
                    "MP_ACCESS_TOKEN": "TOKEN-SECRETO",
                    "MP_WEBHOOK_SECRET": "WEBHOOK-SECRETO",
                }
            )
        )
        for segredo in ("TOKEN-SECRETO", "WEBHOOK-SECRETO", SEGREDO_LINK_DEMO):
            assert segredo not in texto

    def test_producao_exige_enderecos_e_segredo_explicitos(self) -> None:
        with pytest.raises(ValueError, match="ORCAMENTO_LINK_SECRET obrigatoria"):
            Configuracao.do_ambiente({"ENVIRONMENT": "production"})
        base = {
            "ENVIRONMENT": "production",
            "ORCAMENTO_LINK_SECRET": SEGREDO_FORTE,
            "BILLING_PUBLIC_URL": "https://pytstop.exemplo/billing/",
            "JWKS_URL": "http://os-service/.well-known/jwks.json",
        }
        with pytest.raises(ValueError, match="MONGODB_URI obrigatoria"):
            Configuracao.do_ambiente(base)
        config = Configuracao.do_ambiente(
            {**base, "MONGODB_URI": "mongodb://mongo:27017/?replicaSet=rs0"}
        )
        assert config.url_publica == "https://pytstop.exemplo/billing"

    @pytest.mark.parametrize(
        ("segredo", "erro"),
        [(SEGREDO_LINK_DEMO, "demonstracao"), ("curto", ">= 32 bytes")],
    )
    def test_producao_recusa_segredo_fraco(self, segredo: str, erro: str) -> None:
        with pytest.raises(ValueError, match=erro):
            Configuracao.do_ambiente(
                {"ENVIRONMENT": "production", "ORCAMENTO_LINK_SECRET": segredo}
            )

    def test_modo_mercadopago_exige_credenciais(self) -> None:
        with pytest.raises(ValueError, match="MP_ACCESS_TOKEN e MP_WEBHOOK_SECRET"):
            Configuracao.do_ambiente(
                {**DEV, "MP_MODE": "mercadopago", "MP_ACCESS_TOKEN": "x"}
            )
        config = Configuracao.do_ambiente(
            {
                **DEV,
                "MP_MODE": "MercadoPago",
                "MP_ACCESS_TOKEN": "x",
                "MP_WEBHOOK_SECRET": "y",
                "MP_NOTIFICATION_URL": "https://publico/webhook",
                "MP_TIMEOUT_SEGUNDOS": "2.5",
            }
        )
        assert config.mp_modo is ModoMercadoPago.MERCADOPAGO
        assert config.mp_notification_url == "https://publico/webhook"
        assert str(config.mp_timeout_segundos) == "2.5"
        gateway = criar_gateway(config)
        assert isinstance(gateway, MercadoPagoGateway)
        gateway.fechar()

    def test_modo_invalido(self) -> None:
        with pytest.raises(ValueError, match="MP_MODE invalido"):
            Configuracao.do_ambiente({**DEV, "MP_MODE": "paypal"})

    @pytest.mark.parametrize("valor", ["abc", "0", "-1", "inf", "nan"])
    def test_prazos_devem_ser_numeros_finitos_positivos(self, valor: str) -> None:
        with pytest.raises(ValueError, match="ORCAMENTO_VALIDADE_HORAS"):
            Configuracao.do_ambiente({**DEV, "ORCAMENTO_VALIDADE_HORAS": valor})

    def test_prazos_curtos_para_demo(self) -> None:
        config = Configuracao.do_ambiente(
            {
                **DEV,
                "ORCAMENTO_VALIDADE_HORAS": "0.05",
                "PAGAMENTO_VALIDADE_MINUTOS": "2",
            }
        )
        assert config.orcamento_validade == timedelta(minutes=3)
        assert config.pagamento_validade == timedelta(minutes=2)

    def test_gateway_mercadopago_sem_token_e_erro_de_configuracao(self) -> None:
        config = Configuracao.do_ambiente(DEV)
        object.__setattr__(config, "mp_modo", ModoMercadoPago.MERCADOPAGO)
        with pytest.raises(ValueError, match="MP_ACCESS_TOKEN"):
            criar_gateway(config)
