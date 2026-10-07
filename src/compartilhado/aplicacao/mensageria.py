"""Desfecho de um comando da saga, que o handler devolve ao consumidor."""

from __future__ import annotations

from enum import StrEnum


class Desfecho(StrEnum):
    """O que o handler fez com o comando (rotulo ``resultado`` da metrica)."""

    PROCESSADA = "processada"
    # Original atrasado depois da lapide, ou comando que nao cabe no estado
    # atual: sem efeito e sem resposta.
    IGNORADA = "ignorada"
