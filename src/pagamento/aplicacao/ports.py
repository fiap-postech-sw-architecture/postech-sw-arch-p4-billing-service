"""Portas de saida do pagamento: provedor de pagamento e orcamentos.

``GatewayPagamento`` tem dois adapters (RFC-004 §8): ``MercadoPagoGateway``
(Checkout Pro real) e ``GatewayPagamentoSimulado`` (default em dev, CI e
demo). A aplicacao nao sabe qual esta ligado.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from src.compartilhado.dominio.exceptions import (
    DependenciaIndisponivelError,
    DomainException,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from src.compartilhado.dominio.dinheiro import Dinheiro
    from src.pagamento.dominio.pagamento import StatusPagamento


class GatewayPagamentoIndisponivelError(DependenciaIndisponivelError):
    """Falha transitoria (timeout, 5xx, circuito aberto): repetir mais tarde."""

    codigo = "GATEWAY_PAGAMENTO_INDISPONIVEL"
    mensagem_padrao = "Provedor de pagamento indisponivel; tente novamente"


class EstornoEmProcessamentoError(GatewayPagamentoIndisponivelError):
    """O provedor nao confirmou o estorno como concluido (``in_process`` ou
    resposta sem status): conferir na consulta ou repetir com a mesma chave."""

    codigo = "ESTORNO_EM_PROCESSAMENTO"
    mensagem_padrao = "Estorno em processamento no provedor; tente novamente"


class GatewayPagamentoRecusouError(DomainException):
    """O provedor recusou a operacao (4xx): repetir igual nao resolve."""

    codigo = "GATEWAY_PAGAMENTO_RECUSOU"
    mensagem_padrao = "Provedor de pagamento recusou a operacao"


@dataclass(frozen=True, slots=True)
class ItemCobranca:
    codigo: str
    descricao: str
    quantidade: int
    preco_unitario: Dinheiro


@dataclass(frozen=True, slots=True)
class Cobranca:
    referencia: str
    checkout_url: str


@dataclass(frozen=True, slots=True)
class SituacaoNoProvedor:
    """Pagamento como o provedor o ve na consulta (fonte de verdade do status)."""

    referencia: str
    referencia_externa: str | None
    status: StatusPagamento
    status_provedor: str
    detalhe: str | None
    valor: Dinheiro | None


class GatewayPagamento(Protocol):
    @property
    def provedor(self) -> str: ...

    def criar_cobranca(
        self,
        *,
        pagamento_id: UUID,
        itens: Sequence[ItemCobranca],
        expira_em: datetime,
    ) -> Cobranca:
        """Cria a cobranca com ``pagamento_id`` como referencia externa.

        Nao e idempotente: sem retry automatico.
        """
        ...

    def consultar_pagamento(self, referencia: str) -> SituacaoNoProvedor | None:
        """Situacao atual no provedor; ``None`` se a referencia nao existe la."""
        ...

    def estornar(self, referencia: str, *, chave_idempotencia: str) -> None:
        """Estorno total concluido; a chave torna a repeticao segura.

        Levanta ``EstornoEmProcessamentoError`` enquanto o provedor processa e
        ``GatewayPagamentoRecusouError`` quando ele recusa.
        """
        ...


class SimuladorDePagamento(Protocol):
    """Lado "cliente pagando" do provedor simulado (so com MP_MODE=simulado)."""

    def registrar_resultado(
        self, *, pagamento_id: UUID, valor: Dinheiro, aprovado: bool
    ) -> str:
        """Simula o pagamento no provedor e devolve a referencia dele."""
        ...


@dataclass(frozen=True, slots=True)
class OrcamentoParaPagamento:
    ordem_id: UUID
    aprovado: bool
    itens: tuple[ItemCobranca, ...]
    total: Dinheiro


class OrcamentosPort(Protocol):
    def obter(self, orcamento_id: UUID) -> OrcamentoParaPagamento | None: ...
