"""Fabricas do ``GatewayPagamento``: um lugar so para a API, o consumidor dos
comandos e o ``prazos``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.configuracao import ModoMercadoPago
from src.pagamento.infraestrutura.mercadopago import (
    ConfiguracaoMercadoPago,
    MercadoPagoGateway,
)
from src.pagamento.infraestrutura.simulado import (
    CAMINHO_CHECKOUT,
    GatewayPagamentoSimulado,
)

if TYPE_CHECKING:
    from src.configuracao import ConfiguracaoDosComandos, ConfiguracaoDosPrazos
    from src.pagamento.aplicacao.ports import GatewayPagamento


def criar_gateway(config: ConfiguracaoDosComandos) -> GatewayPagamento:
    """``MP_MODE=mercadopago`` liga o Checkout Pro real; ``simulado``, o simulador.

    O simulador assina o ``checkout_url`` com o segredo do link de decisao, em
    dominio proprio (um token nao vale no lugar do outro). A API e o
    consumidor dos comandos usam a mesma fabrica: o token emitido num processo
    vale no outro, e o estorno que o simulador nao conhece e aceito.
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


def criar_gateway_de_conciliacao(
    config: ConfiguracaoDosPrazos,
) -> MercadoPagoGateway | None:
    """So o Mercado Pago real e conciliado: o simulador vive na memoria da API."""
    if config.mp_modo is not ModoMercadoPago.MERCADOPAGO or not config.mp_access_token:
        return None
    return MercadoPagoGateway(
        ConfiguracaoMercadoPago(
            access_token=config.mp_access_token,
            # O prazos so consulta: nunca cria preferencia.
            notification_url="",
            base_url=config.mp_api_url,
            timeout_segundos=config.mp_timeout_segundos,
        )
    )
