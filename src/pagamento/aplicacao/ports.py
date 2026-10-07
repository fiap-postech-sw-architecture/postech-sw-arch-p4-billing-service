"""Portas de saida do pagamento: provedor de pagamento, orcamentos e metricas.

``GatewayPagamento`` tem dois adapters (ADR-040): ``MercadoPagoGateway``
(Checkout Pro real) e ``GatewayPagamentoSimulado`` (CI, compose e E2E). A
aplicacao nao sabe qual esta ligado.
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
    from src.pagamento.dominio.cobranca import SituacaoNoProvedor
    from src.pagamento.dominio.estados import MotivoEstorno


class GatewayPagamentoIndisponivelError(DependenciaIndisponivelError):
    """Falha transitoria (timeout, 5xx, resposta fora do contrato, circuito
    aberto): repetir mais tarde."""

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
class CobrancaCriada:
    """Resposta do provedor a criacao da cobranca (preferencia + checkout)."""

    referencia: str
    checkout_url: str


class GatewayPagamento(Protocol):
    @property
    def provedor(self) -> str: ...

    def criar_cobranca(
        self,
        *,
        pagamento_id: UUID,
        itens: Sequence[ItemCobranca],
        expira_em: datetime,
    ) -> CobrancaCriada:
        """Cria a cobranca com ``pagamento_id`` como referencia externa.

        Nao e idempotente: sem retry automatico.
        """
        ...

    def cancelar_cobranca(self, referencia_preferencia: str) -> None:
        """Fecha o checkout no provedor (idempotente): nada mais e pago nele."""
        ...

    def consultar_pagamento(self, referencia: str) -> SituacaoNoProvedor | None:
        """Situacao atual no provedor; ``None`` se a referencia nao existe la."""
        ...

    def buscar_por_referencia_externa(
        self, referencia_externa: str
    ) -> list[SituacaoNoProvedor]:
        """Tentativas de pagamento da cobranca (``external_reference`` = id do
        pagamento): a conciliacao ativa do processo ``prazos``."""
        ...

    def estornar(self, referencia: str, *, chave_idempotencia: str) -> None:
        """Estorno total concluido; a chave torna a repeticao segura.

        Levanta ``EstornoEmProcessamentoError`` enquanto o provedor processa e
        ``GatewayPagamentoRecusouError`` quando ele recusa.
        """
        ...


class SimuladorDePagamento(Protocol):
    """Lado "cliente pagando" do provedor simulado (so com MP_MODE=simulado)."""

    def checkout_autorizado(
        self, pagamento_id: UUID, token: str | None, *, agora: datetime
    ) -> bool:
        """O token e o do ``checkout_url`` deste pagamento e ainda vale."""
        ...

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


class MetricasDePagamento(Protocol):
    """Contadores do pagamento (``pytstop_pagamentos_estornados_total``, a
    falha do estorno automatico e o checkout que o provedor recusou fechar),
    implementados na infraestrutura.

    Contagem pelo menos uma vez: no consumidor o caso de uso conta antes de a
    transacao da mensagem comitar, e o handler repetido (conflito na
    transacao, copia de retry) conta de novo.
    """

    def estorno_concluido(self, motivo: MotivoEstorno) -> None: ...

    def estorno_automatico_falhou(self) -> None: ...

    def cancelamento_de_cobranca_recusado(self) -> None: ...
