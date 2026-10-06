"""Provedor de pagamento simulado (``MP_MODE=simulado``: dev, CI, compose e E2E).

Imita o Mercado Pago no que o Billing usa: cria a "cobranca" com um checkout
servido pelo proprio Billing e guarda o resultado de cada pagamento simulado
para a consulta, que segue o mesmo caminho do webhook real.

O ``checkout_url`` leva um token assinado (``pagamento_id`` + expiracao da
cobranca), exigido pelas rotas do simulador: sem ele, quem soubesse o id do
pagamento aprovaria a cobranca. O dominio do token e proprio, entao o token do
link de decisao do orcamento (mesmo segredo) nao abre o checkout.

Estado em memoria e suficiente: aprovar/recusar registra o resultado e
processa a notificacao na mesma requisicao (mesmo processo). Um estorno de
pagamento que o simulador nao conhece (processo reiniciado) e aceito.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from src.compartilhado.aplicacao.token_assinado import TokenAssinado
from src.pagamento.aplicacao.ports import CobrancaCriada, GatewayPagamentoRecusouError
from src.pagamento.dominio.cobranca import SituacaoNoProvedor
from src.pagamento.dominio.estados import StatusNoProvedor

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from src.compartilhado.dominio.dinheiro import Dinheiro
    from src.pagamento.aplicacao.ports import ItemCobranca

_DOMINIO_DE_ASSINATURA = "checkout-simulado"
_PREFIXO_PREFERENCIA = "sim-pref-"


class GatewayPagamentoSimulado:
    """Provedor falso: checkout proprio e resultado escolhido no botao."""

    provedor = "simulado"

    def __init__(self, *, url_checkout: str, segredo: str) -> None:
        """``url_checkout`` ja inclui o caminho da pagina de checkout."""
        self._url_checkout = url_checkout.rstrip("/")
        self._token = TokenAssinado(segredo=segredo, dominio=_DOMINIO_DE_ASSINATURA)
        self._pagamentos: dict[str, SituacaoNoProvedor] = {}
        self._trava = threading.Lock()

    def criar_cobranca(
        self,
        *,
        pagamento_id: UUID,
        itens: Sequence[ItemCobranca],
        expira_em: datetime,
    ) -> CobrancaCriada:
        assinado = self._token.emitir(pagamento_id, expira_em)
        return CobrancaCriada(
            referencia=f"{_PREFIXO_PREFERENCIA}{pagamento_id}",
            checkout_url=f"{self._url_checkout}/{pagamento_id}?token={assinado}",
        )

    def cancelar_cobranca(self, referencia_preferencia: str) -> None:
        """Nada a fechar aqui: o checkout simulado so aceita pagamento
        SOLICITADO, e o status gravado vale para todos os processos."""

    def checkout_autorizado(
        self, pagamento_id: UUID, token: str | None, *, agora: datetime
    ) -> bool:
        """O token e o do ``checkout_url`` deste pagamento e ainda vale."""
        return token is not None and self._token.validar(token, agora=agora) == (
            pagamento_id
        )

    def consultar_pagamento(self, referencia: str) -> SituacaoNoProvedor | None:
        with self._trava:
            return self._pagamentos.get(referencia)

    def buscar_por_referencia_externa(
        self, referencia_externa: str
    ) -> list[SituacaoNoProvedor]:
        with self._trava:
            return [
                situacao
                for situacao in self._pagamentos.values()
                if situacao.referencia_externa == referencia_externa
            ]

    def estornar(self, referencia: str, *, chave_idempotencia: str) -> None:
        with self._trava:
            situacao = self._pagamentos.get(referencia)
            if situacao is None or situacao.status is StatusNoProvedor.ESTORNADO:
                return
            if situacao.status is not StatusNoProvedor.APROVADO:
                msg = "Simulador: so pagamento aprovado pode ser estornado"
                raise GatewayPagamentoRecusouError(msg)
            self._pagamentos[referencia] = replace(
                situacao, status=StatusNoProvedor.ESTORNADO, status_provedor="refunded"
            )

    def registrar_resultado(
        self, *, pagamento_id: UUID, valor: Dinheiro, aprovado: bool
    ) -> str:
        referencia = f"sim-{uuid4().hex}"
        situacao = SituacaoNoProvedor(
            referencia=referencia,
            referencia_externa=str(pagamento_id),
            status=StatusNoProvedor.APROVADO if aprovado else StatusNoProvedor.RECUSADO,
            status_provedor="approved" if aprovado else "rejected",
            detalhe="accredited" if aprovado else "cc_rejected_other_reason",
            valor=valor,
        )
        with self._trava:
            self._pagamentos[referencia] = situacao
        return referencia
