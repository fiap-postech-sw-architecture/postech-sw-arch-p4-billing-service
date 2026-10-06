"""Excecoes de dominio; o ``codigo`` estavel vai para o envelope de erro da API.

Cada familia mapeia um status HTTP em ``interfaces/error_handler.py``; as
subclasses dos contextos so trocam ``codigo`` e a mensagem padrao.
"""

from __future__ import annotations

from typing import ClassVar


class DomainException(Exception):
    codigo: ClassVar[str] = "VIOLACAO_REGRA_NEGOCIO"
    mensagem_padrao: ClassVar[str] = "Violacao de regra de negocio"

    def __init__(self, mensagem: str | None = None) -> None:
        self.mensagem = mensagem or self.mensagem_padrao
        super().__init__(self.mensagem)


class EntidadeNaoEncontradaError(DomainException):
    codigo = "ENTIDADE_NAO_ENCONTRADA"
    mensagem_padrao = "Entidade nao encontrada"


class EntidadeDuplicadaError(DomainException):
    codigo = "ENTIDADE_DUPLICADA"
    mensagem_padrao = "Entidade duplicada"


class TransicaoStatusInvalidaError(DomainException):
    codigo = "TRANSICAO_STATUS_INVALIDA"
    mensagem_padrao = "Transicao de status invalida"


class RecursoExpiradoError(DomainException):
    codigo = "RECURSO_EXPIRADO"
    mensagem_padrao = "Recurso expirado"


class DependenciaIndisponivelError(DomainException):
    codigo = "DEPENDENCIA_INDISPONIVEL"
    mensagem_padrao = "Dependencia externa indisponivel; tente novamente"
