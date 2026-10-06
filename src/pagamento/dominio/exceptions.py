from __future__ import annotations

from src.compartilhado.dominio.exceptions import (
    DomainException,
    EntidadeDuplicadaError,
    EntidadeNaoEncontradaError,
)


class PagamentoNaoEncontradoError(EntidadeNaoEncontradaError):
    mensagem_padrao = "Pagamento nao encontrado"


class PagamentoJaSolicitadoError(EntidadeDuplicadaError):
    codigo = "PAGAMENTO_JA_SOLICITADO"
    mensagem_padrao = "Ja existe pagamento para este orcamento"


class OrcamentoNaoAprovadoError(DomainException):
    codigo = "ORCAMENTO_NAO_APROVADO"
    mensagem_padrao = "So orcamento aprovado pode ser cobrado"
