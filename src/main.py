"""Composicao da API do Billing Service (``uvicorn src.main:criar_app --factory``)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI
from prometheus_client import REGISTRY

from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.jwks import ValidadorDeTokenJWKS
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.mensageria import contratos
from src.compartilhado.infraestrutura.mensageria.metricas import ColetorDaOutbox
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    configurar_telemetria,
)
from src.compartilhado.infraestrutura.metricas import configurar_metricas
from src.compartilhado.infraestrutura.mongo import criar_cliente
from src.compartilhado.infraestrutura.unit_of_work import COLECAO_OUTBOX
from src.compartilhado.interfaces.error_handler import registrar_error_handlers
from src.compartilhado.interfaces.middleware import SecurityHeadersMiddleware
from src.compartilhado.interfaces.rastreamento import instrumentar_api
from src.compartilhado.interfaces.router_saude import router as router_saude
from src.configuracao import Configuracao, ModoMercadoPago
from src.orcamento.aplicacao.link_decisao import CAMINHO_DO_LINK, LinkDeDecisao
from src.orcamento.infraestrutura.metricas import MetricasDeOrcamentoPrometheus
from src.orcamento.interfaces.router import router as router_orcamentos
from src.orcamento.interfaces.router_publico import router as router_publico
from src.pagamento.infraestrutura.gateway import criar_gateway
from src.pagamento.infraestrutura.mercadopago import MercadoPagoGateway
from src.pagamento.infraestrutura.metricas import MetricasPrometheus
from src.pagamento.interfaces.router import router as router_pagamentos
from src.pagamento.interfaces.router_simulador import router as router_simulador
from src.precos.interfaces.router import router as router_precos

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from opentelemetry.sdk.trace import TracerProvider
    from pymongo.database import Database

    from src.compartilhado.dominio.relogio import Relogio
    from src.compartilhado.infraestrutura.mongo import Documento
    from src.pagamento.aplicacao.ports import GatewayPagamento


_log = structlog.get_logger(__name__)


def criar_app(
    config: Configuracao | None = None,
    *,
    banco: Database[Documento] | None = None,
    gateway: GatewayPagamento | None = None,
    relogio: Relogio = agora_utc,
    provedor: TracerProvider | None = None,
) -> FastAPI:
    """Monta a API. ``banco``, ``gateway``, ``relogio`` e ``provedor`` injetados
    servem aos testes.

    E a fabrica do uvicorn (``--factory``): o log JSON e configurado aqui, antes
    da primeira linha do servidor, e nao no lifespan. Sem ``provedor``, a API
    instala o do processo (``configurar_telemetria``) e o encerra no fim do
    lifespan, o que descarrega os spans: o uvicorn re-levanta o SIGTERM depois do
    desligamento e o processo morre sem rodar o ``atexit`` do SDK. Quem injeta o
    provider e dono dele.
    """
    configurar_logging()
    config = config or Configuracao.do_ambiente()
    contratos.tipos_com_contrato()  # schemas das mensagens ou falha no boot
    # Antes de criar o cliente do MongoDB (no lifespan): o PyMongo so rastreia
    # os clientes criados depois da instrumentacao.
    dono_do_provedor = provedor is None
    provedor = provedor or configurar_telemetria("api")
    gateway = gateway or criar_gateway(config.comandos)
    producao = config.ambiente == "production"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if producao and config.mp_modo is ModoMercadoPago.SIMULADO:
            # So chega aqui com SIMULADOR_PERMITIDO=true (demo): fica no log.
            _log.warning("payment_simulator_enabled_in_production")
        # Sem acesso ao banco no boot: com o MongoDB fora a API sobe e a
        # readiness responde 503 ate ele voltar (indices e validadores sao do
        # init, ``python -m src.banco``).
        cliente = None if banco is not None else criar_cliente(config.mongodb_uri)
        app.state.banco = banco if cliente is None else cliente[config.mongodb_banco]
        # Idade da outbox na raspagem da API: com o relay parado ela segue
        # exportada, e e ela que mantem o alerta de outbox parada de pe.
        coletor = ColetorDaOutbox(COLECAO_OUTBOX, app.state.banco)
        REGISTRY.register(coletor)
        try:
            yield
        finally:
            REGISTRY.unregister(coletor)
            if cliente is not None:
                cliente.close()
            if isinstance(gateway, MercadoPagoGateway):
                gateway.fechar()
            if dono_do_provedor:
                provedor.shutdown()

    app = FastAPI(
        title="PytStop Billing Service",
        version=version("pytstop-billing-service"),
        description=(
            "Tabela de precos, orcamento e pagamento (Mercado Pago) do PytStop. "
            "Erros no envelope {erro: {codigo, mensagem, id_requisicao}}."
        ),
        lifespan=lifespan,
        # /docs e /redoc em todo ambiente: a borda publica o Swagger (ADR-038).
        # Sem redirect de barra final: 404 direto, sem 307 para outra URL.
        redirect_slashes=False,
        # Sem o export automatico do FastAPI: com o endpoint OTLP no ambiente
        # ele soma exportadores http/protobuf na partida, que pedem o extra
        # fastapi[opentelemetry] (o endpoint do cluster e gRPC) e deixavam um
        # aviso no log de boot. Quem exporta, por gRPC, e o provider do processo
        # (mensageria/telemetria.py), e os spans do FastAPI saem da instrumentacao
        # de rastreamento.py, que desliga a telemetria nativa dele.
        telemetry={"auto_configure": False},
    )
    instrumentar_api(app, provedor)
    _montar_estado(app, config, gateway, relogio)
    _incluir_rotas(app, config)
    app.add_middleware(SecurityHeadersMiddleware)
    registrar_error_handlers(app)
    # Por ultimo: o middleware de metricas fica o mais externo e mede tudo.
    configurar_metricas(app)
    return app


def _montar_estado(
    app: FastAPI, config: Configuracao, gateway: GatewayPagamento, relogio: Relogio
) -> None:
    app.state.config = config
    app.state.relogio = relogio
    app.state.gateway_pagamento = gateway
    app.state.metricas_pagamento = MetricasPrometheus()
    app.state.metricas_orcamento = MetricasDeOrcamentoPrometheus()
    app.state.link_decisao = LinkDeDecisao(
        segredo=config.link_segredo, url_base=f"{config.url_publica}{CAMINHO_DO_LINK}"
    )
    app.state.validador_de_token = ValidadorDeTokenJWKS(config.jwks_url)


def _incluir_rotas(app: FastAPI, config: Configuracao) -> None:
    app.include_router(router_saude)
    app.include_router(router_precos)
    app.include_router(router_orcamentos)
    app.include_router(router_publico)
    app.include_router(router_pagamentos)
    if config.mp_modo is ModoMercadoPago.SIMULADO:
        app.include_router(router_simulador)
