"""Provedor de pagamento simulado (``MP_MODE=simulado``, default em dev/CI/demo).

Imita o Mercado Pago no que o Billing usa: cria a "cobranca" com um checkout
servido pelo proprio Billing (``/simulador/checkout/{pagamento_id}``) e
guarda o resultado de cada pagamento simulado para a consulta, que segue o
mesmo caminho do webhook real.

Estado em memoria e suficiente: aprovar/recusar registra o resultado e
processa a notificacao na mesma requisicao (mesmo processo). Um estorno de
pagamento que o simulador nao conhece (processo reiniciado) e aceito.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from src.pagamento.aplicacao.ports import (
    Cobranca,
    GatewayPagamentoRecusouError,
    SituacaoNoProvedor,
)
from src.pagamento.dominio.pagamento import StatusPagamento

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from src.compartilhado.dominio.dinheiro import Dinheiro
    from src.pagamento.aplicacao.ports import ItemCobranca

CAMINHO_CHECKOUT = "/simulador/checkout"


class GatewayPagamentoSimulado:
    provedor = "simulado"

    def __init__(self, *, url_publica: str) -> None:
        self._url_checkout = f"{url_publica.rstrip('/')}{CAMINHO_CHECKOUT}"
        self._pagamentos: dict[str, SituacaoNoProvedor] = {}
        self._trava = threading.Lock()

    def criar_cobranca(
        self,
        *,
        pagamento_id: UUID,
        itens: Sequence[ItemCobranca],
        expira_em: datetime,
    ) -> Cobranca:
        return Cobranca(
            referencia=f"sim-pref-{pagamento_id}",
            checkout_url=f"{self._url_checkout}/{pagamento_id}",
        )

    def consultar_pagamento(self, referencia: str) -> SituacaoNoProvedor | None:
        with self._trava:
            return self._pagamentos.get(referencia)

    def estornar(self, referencia: str, *, chave_idempotencia: str) -> None:
        with self._trava:
            situacao = self._pagamentos.get(referencia)
            if situacao is None or situacao.status is StatusPagamento.ESTORNADO:
                return
            if situacao.status is not StatusPagamento.APROVADO:
                msg = "Simulador: so pagamento aprovado pode ser estornado"
                raise GatewayPagamentoRecusouError(msg)
            self._pagamentos[referencia] = replace(
                situacao, status=StatusPagamento.ESTORNADO, status_provedor="refunded"
            )

    def registrar_resultado(
        self, *, pagamento_id: UUID, valor: Dinheiro, aprovado: bool
    ) -> str:
        referencia = f"sim-{uuid4().hex}"
        situacao = SituacaoNoProvedor(
            referencia=referencia,
            referencia_externa=str(pagamento_id),
            status=StatusPagamento.APROVADO if aprovado else StatusPagamento.RECUSADO,
            status_provedor="approved" if aprovado else "rejected",
            detalhe="accredited" if aprovado else "cc_rejected_other_reason",
            valor=valor,
        )
        with self._trava:
            self._pagamentos[referencia] = situacao
        return referencia
