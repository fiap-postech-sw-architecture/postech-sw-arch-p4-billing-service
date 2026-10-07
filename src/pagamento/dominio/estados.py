"""Estados do pagamento, o que o provedor diz de cada tentativa e os planos.

Transicoes do agregado (allow-list em ``TRANSICOES``; RFC-004 secao 7.1)::

    SOLICITADO -> CONFIRMADO | RECUSADO | EXPIRADO | CANCELADO
    CONFIRMADO -> ESTORNADO
    RECUSADO | EXPIRADO | CANCELADO -> ESTORNADO (aprovacao tardia estornada)
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final


class StatusPagamento(StrEnum):
    SOLICITADO = "SOLICITADO"
    CONFIRMADO = "CONFIRMADO"
    RECUSADO = "RECUSADO"
    EXPIRADO = "EXPIRADO"
    CANCELADO = "CANCELADO"
    ESTORNADO = "ESTORNADO"


TRANSICOES: Final = MappingProxyType(
    {
        StatusPagamento.SOLICITADO: frozenset(
            {
                StatusPagamento.CONFIRMADO,
                StatusPagamento.RECUSADO,
                StatusPagamento.EXPIRADO,
                StatusPagamento.CANCELADO,
            }
        ),
        StatusPagamento.CONFIRMADO: frozenset({StatusPagamento.ESTORNADO}),
        StatusPagamento.RECUSADO: frozenset({StatusPagamento.ESTORNADO}),
        StatusPagamento.EXPIRADO: frozenset({StatusPagamento.ESTORNADO}),
        StatusPagamento.CANCELADO: frozenset({StatusPagamento.ESTORNADO}),
    }
)

# Cobranca encerrada sem dinheiro recebido: aprovacao que chegar depois volta.
ENCERRADOS_SEM_PAGAMENTO: Final = frozenset(
    {StatusPagamento.RECUSADO, StatusPagamento.EXPIRADO, StatusPagamento.CANCELADO}
)


class StatusNoProvedor(StrEnum):
    """Situacao de UMA tentativa de pagamento, lida na consulta ao provedor.

    No Checkout Pro a cobranca aceita varias tentativas: so ``APROVADO`` e
    ``RECUSADO`` mudam o pagamento aqui; ``ESTORNADO`` (``refunded``) e o
    resto (pendente, em processamento, cancelada, contestada, desconhecida)
    so entram no historico.
    """

    APROVADO = "aprovado"
    RECUSADO = "recusado"
    ESTORNADO = "estornado"
    EM_ANDAMENTO = "em_andamento"


class MotivoEstorno(StrEnum):
    """Enumeracao fechada do ``PagamentoEstornado`` e da metrica (RFC-004, secao 9)."""

    COMPENSACAO = "compensacao"
    PAGAMENTO_APOS_ENCERRAMENTO = "pagamento_apos_encerramento"


class PlanoDeCompensacao(StrEnum):
    """O que ``EstornarPagamento`` faz em cada estado (ADR-040, passo 6)."""

    CANCELAR_COBRANCA = "cancelar_cobranca"
    ESTORNAR_NO_PROVEDOR = "estornar_no_provedor"
    RESPONDER_CANCELADO = "responder_cancelado"
    RESPONDER_ESTORNADO = "responder_estornado"


class ResultadoNotificacao(StrEnum):
    """Efeito de uma tentativa consultada no provedor sobre o pagamento."""

    SEM_MUDANCA = "sem_mudanca"
    REGISTRADA = "registrada"
    CONFIRMADO = "confirmado"
    RECUSA_CONTADA = "recusa_contada"
    RECUSADO = "recusado"
    # Dinheiro entrou numa cobranca que nao o aceita (encerrada ou com valor
    # ou moeda diferentes): o caso de uso estorna no provedor.
    ESTORNO_AUTOMATICO = "estorno_automatico"
