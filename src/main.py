"""Composicao da API do Billing Service (``uvicorn src.main:criar_app --factory``)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI

from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.jwks import ValidadorDeTokenJWKS
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.metricas import configurar_metricas
from src.compartilhado.infraestrutura.mongo import criar_cliente
from src.compartilhado.interfaces.error_handler import registrar_error_handlers
from src.compartilhado.interfaces.middleware import SecurityHeadersMiddleware
from src.compartilhado.interfaces.router_saude import router as router_saude
from src.configuracao import Configuracao, ModoMercadoPago
from src.orcamento.aplicacao.link_decisao import LinkDeDecisao
from src.orcamento.interfaces.router import router as router_orcamentos
from src.orcamento.interfaces.router_publico import PREFIXO as PREFIXO_DO_LINK
from src.orcamento.interfaces.router_publico import router as router_publico
from src.pagamento.infraestrutura.mercadopago import (
    ConfiguracaoMercadoPago,
    MercadoPagoGateway,
)
from src.pagamento.infraestrutura.metricas import MetricasPrometheus
from src.pagamento.infraestrutura.simulado import GatewayPagamentoSimulado
from src.pagamento.interfaces.router import router as router_pagamentos
from src.pagamento.interfaces.router_simulador import CAMINHO_CHECKOUT
from src.pagamento.interfaces.router_simulador import router as router_simulador
from src.precos.interfaces.router import router as router_precos

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pymongo.database import Database

    from src.compartilhado.dominio.relogio import Relogio
    from src.compartilhado.infraestrutura.mongo import Documento
    from src.pagamento.aplicacao.ports import GatewayPagamento


_log = structlog.get_logger(__name__)


def criar_gateway(config: Configuracao) -> GatewayPagamento:
    """``MP_MODE=mercadopago`` liga o Checkout Pro real; ``simulado``, o simulador.

    O simulador assina o ``checkout_url`` com o segredo do link de decisao, em
    dominio proprio (um token nao vale no lugar do outro).
    """
    if config.mp_modo is ModoMercadoPago.MERCADOPAGO:
        if not config.mp_access_token:  # garantido pela Configuracao
            msg = "MP_ACCESS_TOKEN ausente"
            raise ValueError(msg)
        return MercadoPagoGateway(
            ConfiguracaoMercadoPago(
                access_token=config.mp_access_token,
                notification_url=config.mp_notification_url,
                base_url=config.mp_api_url,
                timeout_segundos=config.mp_timeout_segundos,
            )
        )
    return GatewayPagamentoSimulado(
        url_checkout=f"{config.url_publica}{CAMINHO_CHECKOUT}",
        segredo=config.link_segredo,
    )


def criar_app(
    config: Configuracao | None = None,
    *,
    banco: Database[Documento] | None = None,
    gateway: GatewayPagamento | None = None,
    relogio: Relogio = agora_utc,
) -> FastAPI:
    """Monta a API. ``banco``, ``gateway`` e ``relogio`` injetados servem aos testes.

    E a fabrica do uvicorn (``--factory``): o log JSON e configurado aqui, antes
    da primeira linha do servidor, e nao no lifespan.
    """
    configurar_logging()
    config = config or Configuracao.do_ambiente()
    gateway = gateway or criar_gateway(config)
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
        try:
            yield
        finally:
            if cliente is not None:
                cliente.close()
            if isinstance(gateway, MercadoPagoGateway):
                gateway.fechar()

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
    )
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
    app.state.link_decisao = LinkDeDecisao(
        segredo=config.link_segredo, url_base=f"{config.url_publica}{PREFIXO_DO_LINK}"
    )
    app.state.validador_de_token = ValidadorDeTokenJWKS(
        config.jwks_url, emissor=config.jwt_emissor, audiencia=config.jwt_audiencia
    )


def _incluir_rotas(app: FastAPI, config: Configuracao) -> None:
    app.include_router(router_saude)
    app.include_router(router_precos)
    app.include_router(router_orcamentos)
    app.include_router(router_publico)
    app.include_router(router_pagamentos)
    if config.mp_modo is ModoMercadoPago.SIMULADO:
        app.include_router(router_simulador)
