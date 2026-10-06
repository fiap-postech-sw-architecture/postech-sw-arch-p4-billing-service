"""Excecoes de dominio; o ``codigo`` estavel vai para o envelope de erro da API.

Cada familia mapeia um status HTTP em ``interfaces/error_handler.py``; as
subclasses dos contextos so trocam ``codigo`` e a mensagem padrao.
``ValorInvalidoError`` e a invariante de valor (422 ``VALOR_INVALIDO``).
"""

from __future__ import annotations

from typing import ClassVar


class ValorInvalidoError(ValueError):
    """Invariante de value object ou de agregado violada pela entrada (422).

    Classe propria, e nao ``ValueError`` puro, para a API devolver 422 so para
    dado invalido do chamador: ``ValueError`` de biblioteca ou de adapter (ex.:
    corpo que nao e JSON) e defeito do servidor e vira 500.
    """


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
