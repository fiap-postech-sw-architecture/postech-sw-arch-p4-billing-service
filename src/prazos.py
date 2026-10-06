"""Processo ``prazos``: expira orcamentos e pagamentos vencidos em ciclos.

A espera humana expira no dono do dado (RFC-004 §3): o Billing expira o
orcamento sem decisao e o pagamento nao feito. Roda como processo proprio
(``entrypoint.sh prazos``), separado da API; varias replicas sao seguras porque
cada expiracao e uma transacao que rele o agregado.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from src.banco import preparar_banco
from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.mongo import criar_cliente
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.configuracao import Configuracao
from src.orcamento.aplicacao.use_cases import ExpirarOrcamentosVencidos
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.pagamento.aplicacao.use_cases import ExpirarPagamentosVencidos
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository

if TYPE_CHECKING:
    from pymongo.database import Database

    from src.compartilhado.dominio.relogio import Relogio
    from src.compartilhado.infraestrutura.mongo import Documento

_log = logging.getLogger(__name__)


def executar_ciclo(
    banco: Database[Documento], *, relogio: Relogio = agora_utc
) -> tuple[int, int]:
    """Uma rodada; devolve (orcamentos expirados, pagamentos expirados)."""
    uow = MongoUnitOfWork(banco)
    orcamentos = ExpirarOrcamentosVencidos(
        uow, MongoOrcamentoRepository(uow), relogio
    ).executar()
    pagamentos = ExpirarPagamentosVencidos(
        uow, MongoPagamentoRepository(uow), relogio
    ).executar()
    return orcamentos, pagamentos


def main() -> None:  # pragma: no cover - laco do processo; o ciclo tem teste
    configurar_logging()
    config = Configuracao.do_ambiente()
    cliente = criar_cliente(config.mongodb_uri)
    banco = cliente[config.mongodb_banco]
    preparar_banco(banco)
    _log.info("prazos_iniciado", extra={"intervalo": config.prazos_intervalo_segundos})
    while True:
        try:
            orcamentos, pagamentos = executar_ciclo(banco)
            if orcamentos or pagamentos:
                _log.info(
                    "prazos_expirados",
                    extra={"orcamentos": orcamentos, "pagamentos": pagamentos},
                )
        except Exception:  # noqa: BLE001 - o processo sobrevive e tenta no proximo ciclo
            _log.exception("prazos_ciclo_falhou")
        time.sleep(config.prazos_intervalo_segundos)


if __name__ == "__main__":  # pragma: no cover
    main()
