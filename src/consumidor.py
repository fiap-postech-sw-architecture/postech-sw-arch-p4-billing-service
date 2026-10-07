"""Processo ``consumidor``: comandos da saga (``python -m src.consumidor``).

Consome a fila ``billing.comandos`` com declaracao so passiva do que o usuario
``billing`` alcanca (a propria fila e o ``pytstop.retry``; a topologia e do
``definitions.json`` da plataforma), prefetch pequeno e ack manual. Sem
conexao, reconecta com backoff ate 30 s. O arquivo de vida e tocado a cada
mensagem ou segundo ocioso, com ``pronto`` so enquanto consome. SIGTERM: para
de consumir, conclui a mensagem em curso, cancela o consumo (as pre-buscadas
voltam a fila) e fecha as conexoes.
"""

from __future__ import annotations

import logging
import threading
from functools import partial
from typing import TYPE_CHECKING, Final

from prometheus_client import start_http_server

from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.mensageria.amqp import (
    ESPERA_MAXIMA_SEGUNDOS,
    CanalAmqp,
    manter_conectado,
    parametros,
)
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    EXCHANGE_RETRY,
    ConsumidorDeComandos,
)
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    configurar_telemetria,
)
from src.compartilhado.infraestrutura.mongo import conferir_versao, criar_cliente
from src.compartilhado.infraestrutura.processo import instalar_sinais, sinalizar
from src.configuracao import ConfiguracaoDoConsumidor
from src.main import criar_gateway
from src.orcamento.aplicacao.link_decisao import LinkDeDecisao
from src.orcamento.interfaces import comandos as comandos_do_orcamento
from src.orcamento.interfaces.router_publico import PREFIXO as PREFIXO_DO_LINK
from src.pagamento.infraestrutura.mercadopago import MercadoPagoGateway
from src.pagamento.infraestrutura.metricas import MetricasPrometheus
from src.pagamento.interfaces import comandos as comandos_do_pagamento

if TYPE_CHECKING:
    from pathlib import Path

    from src.compartilhado.dominio.relogio import Relogio
    from src.compartilhado.infraestrutura.mensageria.consumidor import Handler
    from src.configuracao import ConfiguracaoDosComandos
    from src.pagamento.aplicacao.ports import GatewayPagamento

_log = logging.getLogger(__name__)

FILA: Final = "billing.comandos"
PREFETCH: Final = 10
INATIVIDADE_SEGUNDOS: Final = 1.0


def criar_handlers(
    config: ConfiguracaoDosComandos,
    *,
    gateway: GatewayPagamento,
    relogio: Relogio = agora_utc,
) -> dict[str, Handler]:
    """Um handler por comando do Billing no catalogo (RFC-004, secao 5.3)."""
    link = LinkDeDecisao(
        segredo=config.link_segredo, url_base=f"{config.url_publica}{PREFIXO_DO_LINK}"
    )
    return {
        "GerarOrcamento": partial(
            comandos_do_orcamento.gerar_orcamento,
            link=link,
            validade=config.orcamento_validade,
            relogio=relogio,
        ),
        "CancelarOrcamento": partial(
            comandos_do_orcamento.cancelar_orcamento, relogio=relogio
        ),
        "SolicitarPagamento": partial(
            comandos_do_pagamento.solicitar_pagamento,
            gateway=gateway,
            validade=config.pagamento_validade,
            relogio=relogio,
        ),
        "EstornarPagamento": partial(
            comandos_do_pagamento.estornar_pagamento,
            gateway=gateway,
            metricas=MetricasPrometheus(),
            relogio=relogio,
        ),
    }


def rodar(
    consumidor: ConsumidorDeComandos,
    canal: CanalAmqp,
    *,
    parar: threading.Event,
    heartbeat: Path,
    inatividade: float = INATIVIDADE_SEGUNDOS,
    espera_maxima: float = ESPERA_MAXIMA_SEGUNDOS,
) -> None:
    """Laco do processo: conecta, consome enquanto conectado, reconecta."""
    manter_conectado(
        canal,
        lambda: _consumir_enquanto_conectado(
            consumidor, canal, parar, heartbeat, inatividade
        ),
        parar=parar,
        heartbeat=heartbeat,
        processo="consumidor",
        espera_maxima=espera_maxima,
    )


def _consumir_enquanto_conectado(
    consumidor: ConsumidorDeComandos,
    canal: CanalAmqp,
    parar: threading.Event,
    heartbeat: Path,
    inatividade: float,
) -> None:
    canal.canal.basic_qos(prefetch_count=PREFETCH)
    for metodo, propriedades, corpo in canal.canal.consume(
        FILA, inactivity_timeout=inatividade
    ):
        sinalizar(heartbeat, pronto=True)
        if metodo is not None:
            consumidor.tratar(canal, metodo.delivery_tag, propriedades, corpo)
        if parar.is_set():
            # As pre-buscadas ainda sem ack voltam para a fila.
            canal.canal.cancel()
            return


def main(parar: threading.Event | None = None) -> None:
    configurar_logging()
    config = ConfiguracaoDoConsumidor.do_ambiente()
    provedor = configurar_telemetria("consumidor")
    if parar is None:
        parar = threading.Event()
        instalar_sinais(parar)
    servidor, _ = start_http_server(config.porta_metricas)
    cliente = criar_cliente(config.banco.mongodb_uri)
    canal = CanalAmqp(
        parametros(config.rabbitmq_url, nome="billing-consumidor"),
        filas=(FILA,),
        exchanges=(EXCHANGE_RETRY,),
    )
    gateway = criar_gateway(config.comandos)
    try:
        banco = cliente[config.banco.mongodb_banco]
        conferir_versao(banco)
        consumidor = ConsumidorDeComandos(
            banco,
            criar_handlers(config.comandos, gateway=gateway),
            fila=FILA,
            usuario=config.rabbitmq_usuario,
        )
        _log.info("consumer_started", extra={"fila": FILA})
        rodar(consumidor, canal, parar=parar, heartbeat=config.heartbeat)
        _log.info("consumer_stopped")
    finally:
        canal.fechar()
        cliente.close()
        if isinstance(gateway, MercadoPagoGateway):
            gateway.fechar()
        servidor.shutdown()
        provedor.shutdown()


if __name__ == "__main__":  # pragma: no cover
    main()
