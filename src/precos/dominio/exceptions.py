from __future__ import annotations

from src.compartilhado.dominio.exceptions import (
    EntidadeDuplicadaError,
    EntidadeNaoEncontradaError,
)


class PrecoNaoEncontradoError(EntidadeNaoEncontradaError):
    mensagem_padrao = "Preco nao encontrado"


class PrecoJaCadastradoError(EntidadeDuplicadaError):
    codigo = "PRECO_JA_CADASTRADO"
    mensagem_padrao = "Codigo ja cadastrado na tabela de precos"
