"""Processo ``prazos``: conciliacao com o provedor e expiracao, em ciclos.

A espera humana expira no dono do dado (RFC-004, secao 4.6): o Billing expira
o orcamento sem decisao e o pagamento nao feito. Antes de expirar, com o
Mercado Pago real, concilia os pagamentos solicitados (ADR-040, passo 4).
Roda como processo proprio (``entrypoint.sh prazos``), separado da API; varias
replicas sao seguras porque cada mudanca e uma transacao que rele o agregado e
so muda o status esperado. SIGTERM encerra no fim do ciclo em curso; o arquivo
de heartbeat e tocado a cada ciclo (liveness) e o gauge marca o ultimo ciclo
concluido (alerta de prazos parado).
"""

from __future__ import annotations

import logging
import signal
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from prometheus_client import Gauge, start_http_server

from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.mongo import conferir_versao, criar_cliente
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.configuracao import ConfiguracaoDosPrazos, ModoMercadoPago
from src.orcamento.aplicacao.use_cases import ExpirarOrcamentosVencidos
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.pagamento.aplicacao.use_cases import (
    ConciliarPagamentos,
    ExpirarPagamentosVencidos,
    ProcessarNotificacaoPagamento,
)
from src.pagamento.infraestrutura.mercadopago import (
    ConfiguracaoMercadoPago,
    MercadoPagoGateway,
)
from src.pagamento.infraestrutura.metricas import MetricasPrometheus
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pymongo.database import Database

    from src.compartilhado.dominio.relogio import Relogio
    from src.compartilhado.infraestrutura.mongo import Documento
    from src.pagamento.aplicacao.ports import GatewayPagamento, MetricasDePagamento

_log = logging.getLogger(__name__)

LIMITE_POR_CICLO: Final = 100

ULTIMO_CICLO = Gauge(
    "pytstop_prazos_ultimo_ciclo_timestamp_seconds",
    "Instante (epoch) do ultimo ciclo concluido do processo prazos.",
)


@dataclass(frozen=True, slots=True)
class ResultadoDoCiclo:
    conciliados: int
    orcamentos_expirados: int
    pagamentos_expirados: int
    limite: int = field(default=LIMITE_POR_CICLO, compare=False)

    @property
    def lote_cheio(self) -> bool:
        """Uma expiracao bateu no limite: ha mais vencidos, repetir sem esperar.

        A conciliacao nao conta: consultar um pagamento nao o tira da lista dos
        solicitados, e repetir na hora so martelaria o provedor com os mesmos.
        """
        return self.limite in (self.orcamentos_expirados, self.pagamentos_expirados)

    @property
    def vazio(self) -> bool:
        return not (
            self.conciliados or self.orcamentos_expirados or self.pagamentos_expirados
        )


def executar_ciclo(
    banco: Database[Documento],
    *,
    gateway: GatewayPagamento | None,
    metricas: MetricasDePagamento,
    max_recusas: int,
    relogio: Relogio = agora_utc,
    limite: int = LIMITE_POR_CICLO,
) -> ResultadoDoCiclo:
    """Um ciclo: concilia (se houver ``gateway``) e expira orcamentos e pagamentos."""
    uow = MongoUnitOfWork(banco, relogio=relogio)
    pagamentos = MongoPagamentoRepository(uow)
    conciliados = 0
    if gateway is not None:
        processar = ProcessarNotificacaoPagamento(
            uow, pagamentos, gateway, metricas, max_recusas, relogio
        )
        conciliados = ConciliarPagamentos(pagamentos, gateway, processar).executar(
            limite=limite
        )
    orcamentos = ExpirarOrcamentosVencidos(
        uow, MongoOrcamentoRepository(uow), relogio
    ).executar(limite=limite)
    expirados = ExpirarPagamentosVencidos(uow, pagamentos, relogio).executar(
        limite=limite
    )
    return ResultadoDoCiclo(conciliados, orcamentos, expirados, limite)


def rodar(
    ciclo: Callable[[], ResultadoDoCiclo],
    *,
    intervalo: float,
    parar: threading.Event,
    heartbeat: Path,
) -> None:
    """Laco do processo: ciclo, heartbeat e gauge; espera o intervalo ou o sinal.

    Lote cheio repete sem esperar. Falha num ciclo (banco fora, por exemplo)
    vai para o log e o laco segue: reiniciar o pod nao conserta o banco, entao
    o heartbeat continua; o gauge so avanca com ciclo concluido.
    """
    while not parar.is_set():
        cheio = False
        try:
            resultado = ciclo()
        except Exception:  # noqa: BLE001 - o processo sobrevive e tenta no proximo ciclo
            _log.exception("deadlines_cycle_failed")
        else:
            cheio = resultado.lote_cheio
            ULTIMO_CICLO.set_to_current_time()
            if not resultado.vazio:
                _log.info(
                    "deadlines_cycle_done",
                    extra={
                        "conciliados": resultado.conciliados,
                        "orcamentos_expirados": resultado.orcamentos_expirados,
                        "pagamentos_expirados": resultado.pagamentos_expirados,
                    },
                )
        heartbeat.touch()
        if not cheio:
            parar.wait(intervalo)


def instalar_sinais(parar: threading.Event) -> None:
    """Como PID 1 sem handler, o processo ignora SIGTERM e morre por SIGKILL."""
    signal.signal(signal.SIGTERM, lambda *_: parar.set())
    signal.signal(signal.SIGINT, lambda *_: parar.set())


def criar_gateway(config: ConfiguracaoDosPrazos) -> MercadoPagoGateway | None:
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


def main(parar: threading.Event | None = None) -> None:
    configurar_logging()
    config = ConfiguracaoDosPrazos.do_ambiente()
    if parar is None:
        parar = threading.Event()
        instalar_sinais(parar)
    start_http_server(config.porta_metricas)
    cliente = criar_cliente(config.banco.mongodb_uri)
    gateway = criar_gateway(config)
    metricas = MetricasPrometheus()
    try:
        banco = cliente[config.banco.mongodb_banco]
        conferir_versao(banco)
        _log.info(
            "deadlines_started",
            extra={
                "intervalo": config.intervalo_segundos,
                "conciliacao": gateway is not None,
            },
        )
        rodar(
            lambda: executar_ciclo(
                banco,
                gateway=gateway,
                metricas=metricas,
                max_recusas=config.pagamento_max_recusas,
            ),
            intervalo=config.intervalo_segundos,
            parar=parar,
            heartbeat=config.heartbeat,
        )
        _log.info("deadlines_stopped")
    finally:
        cliente.close()
        if gateway is not None:
            gateway.fechar()


if __name__ == "__main__":  # pragma: no cover
    main()
