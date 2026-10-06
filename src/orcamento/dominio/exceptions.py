from __future__ import annotations

from src.compartilhado.dominio.exceptions import (
    EntidadeDuplicadaError,
    EntidadeNaoEncontradaError,
    RecursoExpiradoError,
)


class OrcamentoNaoEncontradoError(EntidadeNaoEncontradaError):
    codigo = "ORCAMENTO_NAO_ENCONTRADO"
    mensagem_padrao = "Orcamento nao encontrado"


class OrcamentoJaGeradoError(EntidadeDuplicadaError):
    codigo = "ORCAMENTO_JA_GERADO"
    mensagem_padrao = "Ja existe orcamento para esta ordem de servico"


class OrcamentoVencidoError(RecursoExpiradoError):
    codigo = "ORCAMENTO_VENCIDO"
    mensagem_padrao = "Prazo de decisao do orcamento esgotado"


class LinkDeDecisaoInvalidoError(EntidadeNaoEncontradaError):
    """Mesmo 404 para token adulterado, expirado, orcamento inexistente ou ja
    decidido: o link nao revela o que falhou (ADR-039)."""

    codigo = "LINK_DECISAO_INVALIDO"
    mensagem_padrao = "Link de decisao invalido, expirado ou ja utilizado"
