from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from src.configuracao import SEGREDO_LINK_DEMO, Configuracao, ModoMercadoPago
from src.main import criar_gateway
from src.pagamento.aplicacao.ports import GatewayPagamentoRecusouError
from src.pagamento.dominio.estados import StatusNoProvedor
from src.pagamento.infraestrutura.mercadopago import MercadoPagoGateway
from src.pagamento.infraestrutura.simulado import GatewayPagamentoSimulado
from tests.factories import AGORA, dinheiro

SEGREDO_FORTE = "3f1c0e5b8a7d4c2e9f6b1a0d8c7e5f4a"
EXPIRA_EM = AGORA + timedelta(minutes=60)


class TestGatewaySimulado:
    @pytest.fixture
    def simulado(self) -> GatewayPagamentoSimulado:
        return GatewayPagamentoSimulado(
            url_checkout="http://billing.teste/simulador/checkout/",
            segredo=SEGREDO_FORTE,
        )

    def test_cobranca_aponta_para_o_checkout_com_token(
        self, simulado: GatewayPagamentoSimulado
    ) -> None:
        pagamento_id = uuid4()
        cobranca = simulado.criar_cobranca(
            pagamento_id=pagamento_id, itens=[], expira_em=EXPIRA_EM
        )
        assert cobranca.referencia == f"sim-pref-{pagamento_id}"
        url = urlsplit(cobranca.checkout_url)
        assert f"{url.scheme}://{url.netloc}{url.path}" == (
            f"http://billing.teste/simulador/checkout/{pagamento_id}"
        )
        [token] = parse_qs(url.query)["token"]
        assert simulado.checkout_autorizado(pagamento_id, token, agora=EXPIRA_EM)

    def test_checkout_so_com_o_token_do_proprio_pagamento_e_no_prazo(
        self, simulado: GatewayPagamentoSimulado
    ) -> None:
        pagamento_id = uuid4()
        cobranca = simulado.criar_cobranca(
            pagamento_id=pagamento_id, itens=[], expira_em=EXPIRA_EM
        )
        [token] = parse_qs(urlsplit(cobranca.checkout_url).query)["token"]
        depois = EXPIRA_EM + timedelta(seconds=1)
        assert not simulado.checkout_autorizado(pagamento_id, token, agora=depois)
        assert not simulado.checkout_autorizado(uuid4(), token, agora=AGORA)
        assert not simulado.checkout_autorizado(pagamento_id, None, agora=AGORA)
        assert not simulado.checkout_autorizado(pagamento_id, "x.y.z", agora=AGORA)

    @pytest.mark.parametrize(
        ("aprovado", "status", "bruto"),
        [
            (True, StatusNoProvedor.APROVADO, "approved"),
            (False, StatusNoProvedor.RECUSADO, "rejected"),
        ],
        ids=["aprovado", "recusado"],
    )
    def test_resultado_simulado_aparece_na_consulta(
        self,
        simulado: GatewayPagamentoSimulado,
        aprovado: bool,
        status: StatusNoProvedor,
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
        assert situacao.status is StatusNoProvedor.ESTORNADO
        # Pagamento anterior a um restart do processo: aceito.
        simulado.estornar("sim-perdido", chave_idempotencia="k")

    def test_cancelar_cobranca_nao_tem_estado_a_fechar(
        self, simulado: GatewayPagamentoSimulado
    ) -> None:
        # O checkout simulado so aceita pagamento SOLICITADO (status gravado).
        assert simulado.cancelar_cobranca("sim-pref-qualquer") is None

    def test_estorno_de_recusado_e_recusado(
        self, simulado: GatewayPagamentoSimulado
    ) -> None:
        recusado = simulado.registrar_resultado(
            pagamento_id=uuid4(), valor=dinheiro("1.00"), aprovado=False
        )
        with pytest.raises(GatewayPagamentoRecusouError):
            simulado.estornar(recusado, chave_idempotencia="k")


DEV = {"ENVIRONMENT": "development", "MP_MODE": "simulado"}
PRODUCAO = {
    "ENVIRONMENT": "production",
    "MP_MODE": "mercadopago",
    "MP_ACCESS_TOKEN": "TEST-token-de-teste",  # gitleaks:allow
    "MP_WEBHOOK_SECRET": "segredo-do-webhook",
    "ORCAMENTO_LINK_SECRET": SEGREDO_FORTE,
    "BILLING_PUBLIC_URL": "https://pytstop.exemplo/billing/",
    "JWKS_URL": "http://os-service:8000/.well-known/jwks.json",
    "MONGODB_URI": "mongodb://mongo:27017/?replicaSet=rs0",
}


class TestConfiguracao:
    def test_sem_environment_assume_producao(self) -> None:
        with pytest.raises(ValueError, match="ENVIRONMENT=production"):
            Configuracao.do_ambiente({"MP_MODE": "mercadopago"})

    @pytest.mark.parametrize("valor", ["prod", "staging", "dev", ""])
    def test_environment_fora_da_lista_e_recusado(self, valor: str) -> None:
        with pytest.raises(ValueError, match="ENVIRONMENT invalido"):
            Configuracao.do_ambiente({**DEV, "ENVIRONMENT": valor})

    def test_environment_ignora_caixa_e_espacos(self) -> None:
        config = Configuracao.do_ambiente({**DEV, "ENVIRONMENT": " Test "})
        assert config.ambiente == "test"

    @pytest.mark.parametrize("ambiente", ["development", "production"])
    def test_mp_mode_e_obrigatorio_sem_padrao(self, ambiente: str) -> None:
        env = {**PRODUCAO, "ENVIRONMENT": ambiente}
        del env["MP_MODE"]
        with pytest.raises(ValueError, match="MP_MODE obrigatoria"):
            Configuracao.do_ambiente(env)

    @pytest.mark.parametrize("permitido", [None, "false", "1", "sim"])
    def test_producao_recusa_o_simulador_sem_permissao_explicita(
        self, permitido: str | None
    ) -> None:
        env = {**PRODUCAO, "MP_MODE": "simulado"}
        if permitido is not None:
            env["SIMULADOR_PERMITIDO"] = permitido
        with pytest.raises(ValueError, match="MP_MODE=simulado recusado"):
            Configuracao.do_ambiente(env)

    def test_producao_aceita_o_simulador_com_permissao_explicita(self) -> None:
        config = Configuracao.do_ambiente(
            {**PRODUCAO, "MP_MODE": "simulado", "SIMULADOR_PERMITIDO": "TRUE"}
        )
        assert config.mp_modo is ModoMercadoPago.SIMULADO

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

    @pytest.mark.parametrize(
        "ausente", ["ORCAMENTO_LINK_SECRET", "MONGODB_URI", "JWKS_URL"]
    )
    def test_producao_exige_enderecos_e_segredo_explicitos(self, ausente: str) -> None:
        env = dict(PRODUCAO)
        del env[ausente]
        with pytest.raises(ValueError, match=f"{ausente} obrigatoria"):
            Configuracao.do_ambiente(env)

    def test_producao_completa(self) -> None:
        config = Configuracao.do_ambiente(PRODUCAO)
        assert config.ambiente == "production"
        assert config.url_publica == "https://pytstop.exemplo/billing"

    @pytest.mark.parametrize(
        ("segredo", "erro"),
        [(SEGREDO_LINK_DEMO, "demonstracao"), ("curto", ">= 32 bytes")],
    )
    def test_producao_recusa_segredo_fraco(self, segredo: str, erro: str) -> None:
        with pytest.raises(ValueError, match=erro):
            Configuracao.do_ambiente({**PRODUCAO, "ORCAMENTO_LINK_SECRET": segredo})

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

    @pytest.mark.parametrize("valor", ["abc", "0", "-1", "1.5"])
    def test_maximo_de_recusas_e_inteiro_positivo(self, valor: str) -> None:
        with pytest.raises(ValueError, match="PAGAMENTO_MAX_RECUSAS"):
            Configuracao.do_ambiente({**DEV, "PAGAMENTO_MAX_RECUSAS": valor})

    def test_maximo_de_recusas_padrao_3_e_configuravel(self) -> None:
        assert Configuracao.do_ambiente(DEV).pagamento_max_recusas == 3
        config = Configuracao.do_ambiente({**DEV, "PAGAMENTO_MAX_RECUSAS": "1"})
        assert config.pagamento_max_recusas == 1

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
